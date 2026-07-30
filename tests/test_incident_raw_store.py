"""Retained raw log text (UAT internal test only).

Two properties carry the weight:

1. **The flag changes only who can read the ORIGINAL — never what the model receives.** If turning
   retention on also put raw text into the packet, it would flow into the model's context and, via
   the replayed transcript, into every following turn of the conversation. That is the thing the
   whole design exists to prevent, and a testing convenience must not quietly undo it.
2. **Reads are owner-scoped**, and a wrong owner is indistinguishable from a missing ref. Otherwise
   one tester's browser could enumerate another's production logs from a shared deployment.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from webapp import config, incident_raw_store as store


LINES = ["2026-07-30 03:15:01 ERROR SmsDeliveryException alice.wong@example.com failed",
         "2026-07-30 03:15:02 WARN  retry for 9123 4567"]


class _Store(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "incident_raw.json")
        self._patchers = [
            mock.patch.object(config, "INCIDENT_RAW_STORE", path),
            mock.patch.object(config, "INCIDENT_RAW_LOGS", True),
            mock.patch.object(config, "INCIDENT_RAW_MAX_ENTRIES", 200),
            mock.patch.object(config, "INCIDENT_RAW_TTL_HOURS", 72),
            mock.patch.object(config, "INCIDENT_RAW_MAX_LINES", 500),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.path = path

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        self._tmp.cleanup()


class RetentionTests(_Store):
    def test_a_stored_entry_round_trips_for_its_owner(self):
        ref = store.put("uid-a", LINES, meta={"app": "cslSmsDeli", "source": "hk1"})
        entry = store.get(ref, "uid-a")
        self.assertEqual(entry["lines"], LINES)
        self.assertEqual(entry["meta"]["app"], "cslSmsDeli")
        self.assertIn("unredacted", entry["warning"].lower())

    def test_another_owner_gets_nothing_and_cannot_tell_it_exists(self):
        ref = store.put("uid-a", LINES)
        self.assertIsNone(store.get(ref, "uid-b"))
        self.assertIsNone(store.get(ref, ""))
        self.assertIsNone(store.get("made-up-ref", "uid-a"))

    def test_with_the_flag_off_nothing_is_stored_and_put_returns_empty(self):
        """An investigation must run identically either way; the only difference is the ref."""
        with mock.patch.object(config, "INCIDENT_RAW_LOGS", False):
            ref = store.put("uid-a", LINES)
        self.assertEqual(ref, "")
        self.assertFalse(os.path.exists(self.path))

    def test_an_entry_stored_while_on_is_unreadable_once_the_flag_goes_off(self):
        ref = store.put("uid-a", LINES)
        with mock.patch.object(config, "INCIDENT_RAW_LOGS", False):
            self.assertIsNone(store.get(ref, "uid-a"))
            self.assertFalse(store.status()["enabled"])

    def test_entries_past_the_ttl_are_refused(self):
        ref = store.put("uid-a", LINES)
        stale = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat().replace(
            "+00:00", "Z")
        data = json.load(open(self.path, encoding="utf-8"))
        data["entries"][0]["stored_at"] = stale
        json.dump(data, open(self.path, "w", encoding="utf-8"))
        self.assertIsNone(store.get(ref, "uid-a"))

    def test_the_oldest_entries_are_dropped_past_the_cap(self):
        with mock.patch.object(config, "INCIDENT_RAW_MAX_ENTRIES", 3):
            refs = [store.put("uid-a", [f"line {i}"]) for i in range(5)]
        self.assertIsNone(store.get(refs[0], "uid-a"))
        self.assertIsNotNone(store.get(refs[-1], "uid-a"))
        self.assertEqual(store.status()["entries"], 3)

    def test_a_long_response_is_line_capped_and_says_so(self):
        with mock.patch.object(config, "INCIDENT_RAW_MAX_LINES", 10):
            ref = store.put("uid-a", [f"line {i}" for i in range(50)])
        entry = store.get(ref, "uid-a")
        self.assertEqual(len(entry["lines"]), 10)
        self.assertEqual(entry["line_count"], 50)
        self.assertTrue(entry["truncated"])

    def test_purge_clears_one_owner_or_everything(self):
        """Retained production logs need a delete, not only a TTL."""
        mine = store.put("uid-a", LINES)
        theirs = store.put("uid-b", LINES)
        self.assertEqual(store.purge("uid-a"), 1)
        self.assertIsNone(store.get(mine, "uid-a"))
        self.assertIsNotNone(store.get(theirs, "uid-b"))
        self.assertEqual(store.purge(), 1)
        self.assertEqual(store.status()["entries"], 0)

    def test_a_corrupt_store_does_not_break_an_investigation(self):
        """Losing retained text costs a click-through; refusing to investigate costs the incident."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(store.get("anything", "uid-a"))
        self.assertTrue(store.put("uid-a", LINES))

    def test_status_warns_loudly_while_retention_is_on(self):
        report = store.status()
        self.assertTrue(report["enabled"])
        self.assertIn("UNREDACTED", report["warning"])
        self.assertIn("turned off", report["warning"])
        self.assertEqual(report["ttl_hours"], 72)


class ModelStillNeverSeesRawTextTests(_Store):
    """The property the flag must not weaken."""

    def setUp(self):
        super().setUp()
        from webapp import incident_investigator as inv, mcp_client
        self.inv = inv
        dirty = "\n".join(LINES)
        self._extra = [
            mock.patch.object(config, "MCP_ENABLED", True),
            mock.patch.object(mcp_client, "call", lambda op, args=None, **k: (
                {"ok": True, "text": '["cslSmsDeli"]'} if op == "log.list_apps"
                else {"ok": True, "text": dirty, "elapsed_ms": 12})),
            mock.patch.object(inv.rcode, "search_code", lambda *a, **k: []),
            mock.patch.object(inv.incident, "parse_alert", lambda *a, **k: {
                "identified": True,
                "repos": [{"repo": "mc-hk-hase-csl-sms-deli-job", "confidence": "confirmed"}],
                "use_cases": [], "metric": "CPUUtilization", "notes": [],
                "times": [{"text": "03:15 HKT", "timezone": "Asia/Hong_Kong"}]}),
        ]
        for patcher in self._extra:
            patcher.start()

    def tearDown(self):
        for patcher in self._extra:
            patcher.stop()
        super().tearDown()

    def test_the_packet_carries_a_ref_but_never_the_raw_text(self):
        packet = self.inv.investigate("alert", owner="uid-a")
        blob = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("alice.wong@example.com", blob)
        self.assertNotIn("9123 4567", blob)
        self.assertTrue(packet["evidence"][0]["raw_ref"])

    def test_the_streamed_events_carry_a_ref_but_never_the_raw_text(self):
        events = list(self.inv.investigate_events("alert", owner="uid-a"))
        steps = [e for e in events if e.get("type") == "subagent_step"]
        blob = json.dumps(steps, ensure_ascii=False)
        self.assertNotIn("alice.wong@example.com", blob)
        hit = next(e for e in steps if e["step"] == "evidence")
        self.assertTrue(hit["detail"]["raw_ref"])

    def test_the_ref_resolves_to_the_original_for_that_owner_only(self):
        packet = self.inv.investigate("alert", owner="uid-a")
        ref = packet["evidence"][0]["raw_ref"]
        self.assertIn("alice.wong@example.com", "\n".join(store.get(ref, "uid-a")["lines"]))
        self.assertIsNone(store.get(ref, "uid-b"))

    def test_the_packet_tells_the_model_it_cannot_read_the_original_itself(self):
        packet = self.inv.investigate("alert", owner="uid-a")
        self.assertIn("YOU cannot", packet["storage_rule"])
        self.assertIn("cannot read it", packet["evidence"][0]["raw_ref_note"])
        self.assertTrue(packet["raw_retention"]["enabled"])

    def test_with_retention_off_there_is_no_ref_and_the_rule_says_the_text_is_gone(self):
        with mock.patch.object(config, "INCIDENT_RAW_LOGS", False):
            packet = self.inv.investigate("alert", owner="uid-a")
        self.assertEqual(packet["evidence"][0]["raw_ref"], "")
        self.assertIn("is gone", packet["storage_rule"])

    def test_a_ref_always_survives_the_exit_gate(self):
        """Regression: `uuid4().hex` often contains 8 consecutive digits, which the phone/account
        patterns match — the gate was eating about one ref in eight, silently breaking click-through
        AND inflating `sanitized_at_exit`, the counter meant to flag a REAL redaction bug. Looped,
        because a single run reproduces it only sometimes."""
        for _ in range(60):
            packet = self.inv.investigate("alert", owner="uid-a")
            for item in packet["evidence"]:
                ref = item["raw_ref"]
                self.assertRegex(ref, r"^[0-9a-f]{32}$", "ref was mangled at the exit gate")
                self.assertIsNotNone(store.get(ref, "uid-a"))
            self.assertEqual(packet["exit_check"]["sanitized_at_exit"], 0)

    def test_the_exemption_does_not_extend_to_content_fields(self):
        """Only machine-generated identifiers are exempt; anything log-derived is still scanned."""
        self.assertEqual(self.inv._IDENTIFIER_KEYS, frozenset({"raw_ref"}))
        cleaned, report = self.inv.sanitize_packet(
            {"raw_ref": "3047878912345678abcd", "excerpts": ["leaked bob@example.com"]})
        self.assertEqual(cleaned["raw_ref"], "3047878912345678abcd")
        self.assertEqual(report["sanitized_at_exit"], 1)
        self.assertNotIn("bob@example.com", json.dumps(cleaned))

    def test_a_cli_investigation_retains_nothing_readable(self):
        """No browser means no owner means nobody can click through — the right default."""
        packet = self.inv.investigate("alert")
        ref = packet["evidence"][0]["raw_ref"]
        self.assertIsNone(store.get(ref, "uid-a"))


if __name__ == "__main__":
    unittest.main()
