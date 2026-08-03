"""Atomic JSON writes on Windows.

The intranet reported an intermittent `os.replace` failure (RUNBOOK-61 send-back, 2026-07-31):
"900 passed / 1 Windows os.replace 偶发失败, 该测试单独运行通过". A flaky test is the cheap
symptom; the same race in the running app drops a chat session, a route registration, or a retained
log entry, and the standing rule is that persisted state is never dropped.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from webapp import atomic_json, incident_raw_store, llm_routes, session_store


class WriteJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "nested", "store.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_it_writes_the_payload_and_creates_the_parent(self):
        atomic_json.write_json(self.path, {"sessions": [{"id": "a"}]})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"sessions": [{"id": "a"}]})

    def test_non_ascii_survives(self):
        atomic_json.write_json(self.path, {"note": "缺时区"})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["note"], "缺时区")

    def test_the_temp_name_is_per_process_so_two_writers_cannot_share_one(self):
        """A fixed `<store>.tmp` let two processes truncate each other's half-written file — rare,
        but the outcome is a corrupt store rather than a retry."""
        seen = {}
        real_open = open

        def _spy(path, *a, **k):
            if str(path).endswith(".tmp"):
                seen["temp"] = str(path)
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", _spy):
            atomic_json.write_json(self.path, {"x": 1})
        self.assertIn(str(os.getpid()), seen["temp"])

    def test_a_transient_permission_error_is_retried_rather_than_lost(self):
        """This is the Windows case: an antivirus or indexer holds the destination for a few ms."""
        calls = {"n": 0}
        real_replace = os.replace

        def _flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "being used by another process")
            return real_replace(src, dst)

        with mock.patch.object(atomic_json.os, "replace", _flaky), \
             mock.patch.object(atomic_json.time, "sleep", lambda _s: None):
            atomic_json.write_json(self.path, {"x": 1})
        self.assertEqual(calls["n"], 3)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"x": 1})

    def test_a_permanent_permission_error_is_raised_never_swallowed(self):
        """A write that truly cannot land has to be visible. Silently returning would be the same
        class of bug as reporting a failed query as 'nothing found'."""
        def _blocked(_src, _dst):
            raise PermissionError(5, "being used by another process")

        with mock.patch.object(atomic_json.os, "replace", _blocked), \
             mock.patch.object(atomic_json.time, "sleep", lambda _s: None):
            with self.assertRaises(PermissionError):
                atomic_json.write_json(self.path, {"x": 1})

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        """A stray `<store>.1234.tmp` next to a store is what someone later mistakes for a backup."""
        def _blocked(_src, _dst):
            raise PermissionError(5, "being used by another process")

        with mock.patch.object(atomic_json.os, "replace", _blocked), \
             mock.patch.object(atomic_json.time, "sleep", lambda _s: None):
            with self.assertRaises(PermissionError):
                atomic_json.write_json(self.path, {"x": 1})
        leftovers = [n for n in os.listdir(os.path.dirname(self.path)) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_two_writes_from_one_process_never_reuse_a_temp_name(self):
        """A per-process name meant a scanner still holding the PREVIOUS temp file blocked the next
        write's open() rather than its replace() — a second way to lose the same write."""
        seen = []
        real_open = open

        def _spy(path, *a, **k):
            if str(path).endswith(".tmp"):
                seen.append(str(path))
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", _spy):
            atomic_json.write_json(self.path, {"x": 1})
            atomic_json.write_json(self.path, {"x": 2})
        self.assertEqual(len(set(seen)), 2, seen)

    def test_a_blocked_temp_file_open_is_retried_too(self):
        """The intranet hit this again after the first fix (RUNBOOK-63 send-back): the scanner
        holds the file we just closed, so both ends of the write need the retry."""
        calls = {"n": 0}
        real_open = open

        def _flaky(path, *a, **k):
            if str(path).endswith(".tmp"):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(5, "being used by another process")
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", _flaky), \
             mock.patch.object(atomic_json.time, "sleep", lambda _s: None):
            atomic_json.write_json(self.path, {"x": 1})
        self.assertEqual(calls["n"], 3)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"x": 1})

    def test_a_non_permission_oserror_is_not_sat_on_for_seconds(self):
        """No such directory or a full disk is a real problem, not a transient lock."""
        waits = []
        with mock.patch.object(atomic_json.os, "replace",
                               mock.Mock(side_effect=FileNotFoundError(2, "gone"))), \
             mock.patch.object(atomic_json.time, "sleep", waits.append):
            with self.assertRaises(FileNotFoundError):
                atomic_json.write_json(self.path, {"x": 1})
        self.assertEqual(waits, [])

    def test_the_retry_budget_is_wide_enough_for_a_corporate_scanner(self):
        """0.5s was not enough — the box still lost one write in a full run. Bounded, but wider."""
        self.assertGreaterEqual(atomic_json._ATTEMPTS, 8)
        total = sum(atomic_json._BACKOFF_SECONDS * n for n in range(1, atomic_json._ATTEMPTS))
        self.assertGreater(total, 1.0)
        self.assertLess(total, 5.0)

    def test_the_backoff_grows_so_a_busy_file_gets_more_than_one_quick_look(self):
        waits = []
        with mock.patch.object(atomic_json.os, "replace",
                               mock.Mock(side_effect=PermissionError(5, "busy"))), \
             mock.patch.object(atomic_json.time, "sleep", waits.append):
            with self.assertRaises(PermissionError):
                atomic_json.write_json(self.path, {"x": 1})
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(len(waits), 1)


class EveryPersistedStoreUsesItTests(unittest.TestCase):
    """All three stores hold state the owner said must never be dropped, so none may keep its own
    hand-rolled replace."""

    def test_no_store_calls_os_replace_directly_any_more(self):
        for module in (session_store, incident_raw_store, llm_routes):
            # `with`, not a bare open(): the un-closed handle raised a ResourceWarning the intranet
            # picked up in RUNBOOK-65, and on Windows a lingering handle is exactly what the retry
            # logic under test exists to survive — a test must not add one.
            with open(module.__file__, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("os.replace", source, module.__name__)
            self.assertIn("atomic_json", source, module.__name__)


if __name__ == "__main__":
    unittest.main()
