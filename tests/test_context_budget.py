"""Context budget: structure-aware tool-result shrinking + history trimming.

The last class is the one that matters for an upgrade: **nothing here may change what is stored**.
An existing webapp_data/chat_sessions.json written by the previous version must keep loading, keep
its unknown fields, and keep round-tripping — a new build must never rewrite or shorten someone's
saved history.
"""
import json
import os
import tempfile
import unittest
from copy import deepcopy
from unittest import mock

from webapp import agent, config, context_budget, session_store


class ShrinkToolResultTests(unittest.TestCase):
    def test_small_result_is_byte_identical_to_plain_dumps(self):
        result = {"ok": True, "items": [1, 2, 3]}
        self.assertEqual(context_budget.shrink_tool_result(result),
                         json.dumps(result, ensure_ascii=False))

    def test_output_is_always_valid_json(self):
        for result in (
            {"items": [{"x": "y" * 500} for _ in range(400)]},
            {"log": "z" * 500000},
            [{"a": "b" * 900} for _ in range(300)],
            {"nested": {"deep": {"rows": list(range(50000))}}},
        ):
            text = context_budget.shrink_tool_result(result, cap=2000)
            json.loads(text)                       # must not raise
            self.assertLessEqual(len(text), 2000)

    def test_long_string_is_capped_with_an_explicit_marker(self):
        out = json.loads(context_budget.shrink_tool_result(
            {"ok": True, "log": "x" * 50000}, cap=4000, string_cap=500))
        self.assertTrue(out["log"].endswith("chars total]"))
        self.assertIn("50000", out["log"])
        self.assertTrue(any(n["kind"] == "string" for n in out["_truncated"]))

    def test_long_list_is_shortened_and_the_loss_is_recorded(self):
        out = json.loads(context_budget.shrink_tool_result(
            {"ok": True, "use_cases": [{"id": f"C{i}"} for i in range(2000)]}, cap=3000))
        self.assertLess(len(out["use_cases"]), 2000)
        note = next(n for n in out["_truncated"] if n["kind"] == "list")
        self.assertEqual(note["total"], 2000)
        self.assertEqual(note["kept"], len(out["use_cases"]))
        self.assertIn("use_cases", note["path"])

    def test_indivisible_payload_falls_back_to_a_self_describing_envelope(self):
        out = json.loads(context_budget.shrink_tool_result({"blob": "q" * 200000}, cap=1200,
                                                            string_cap=190000))
        self.assertTrue(out["_truncated"])
        self.assertIn("PREVIEW", out["_note"])
        self.assertIn("narrow the query", out["_note"])

    def test_a_byte_slice_would_have_been_invalid_json_but_this_is_not(self):
        """The actual regression: the old code did dumps(result)[:cap]."""
        result = {"items": [{"id": i, "name": "n" * 100} for i in range(100)]}
        naive = json.dumps(result, ensure_ascii=False)[:1500]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(naive)
        json.loads(context_budget.shrink_tool_result(result, cap=1500))

    def test_top_level_list_is_wrapped_rather_than_losing_the_truncation_note(self):
        out = json.loads(context_budget.shrink_tool_result(
            [{"id": i, "pad": "p" * 200} for i in range(500)], cap=2000))
        self.assertIn("_result", out)
        self.assertTrue(out["_truncated"])

    def test_unserializable_result_does_not_raise(self):
        out = json.loads(context_budget.shrink_tool_result({"fn": object()}, cap=1000))
        self.assertTrue(out["_truncated"])


class FitHistoryTests(unittest.TestCase):
    def _history(self, turns, size=10):
        out = []
        for i in range(turns):
            out.append({"role": "user", "content": f"q{i} " + "x" * size})
            out.append({"role": "assistant", "content": f"a{i} " + "y" * size})
        return out

    def test_ordinary_history_is_returned_untouched(self):
        history = self._history(5)
        kept, dropped = context_budget.fit_history(history)
        self.assertEqual(kept, history)
        self.assertEqual(dropped, 0)

    def test_oldest_turns_go_first_and_the_recent_ones_survive(self):
        history = self._history(40, size=200)
        kept, dropped = context_budget.fit_history(history, budget=3000)
        self.assertGreater(dropped, 0)
        self.assertEqual(kept[-1], history[-1])
        self.assertLessEqual(sum(len(m["content"]) + 32 for m in kept), 3000)

    def test_the_opening_question_is_always_kept(self):
        history = self._history(40, size=200)
        kept, _ = context_budget.fit_history(history, budget=3000)
        self.assertEqual(kept[0], history[0])

    def test_zero_budget_restores_the_old_unbounded_behaviour(self):
        history = self._history(50, size=500)
        kept, dropped = context_budget.fit_history(history, budget=0)
        self.assertEqual(kept, history)
        self.assertEqual(dropped, 0)

    def test_empty_history_is_fine(self):
        self.assertEqual(context_budget.fit_history([]), ([], 0))


class AgentWiringTests(unittest.TestCase):
    """The agent must behave EXACTLY as before for a normal-length conversation."""

    def _run(self, history):
        captured = []

        def fake_chat_stream(messages, tool_schemas=None):
            captured.append(deepcopy(messages))
            yield ("final", {"role": "assistant", "content": "answer"})

        with mock.patch.object(agent.llm, "chat_stream", fake_chat_stream):
            agent.answer("the question", history)
        return captured[0]

    def test_short_conversation_gets_no_context_notice(self):
        sent = self._run([{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "hello"}])
        self.assertEqual(len(sent), 4)                      # system + 2 history + question
        self.assertEqual(sent[1]["content"], "hi")
        self.assertNotIn("[context]", json.dumps(sent))

    def test_runaway_conversation_is_trimmed_and_the_model_is_told(self):
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "z" * 4000}
                   for i in range(200)]
        with mock.patch.object(config, "HISTORY_CHAR_BUDGET", 20000):
            sent = self._run(history)
        notice = [m for m in sent if m["role"] == "system" and "[context]" in (m["content"] or "")]
        self.assertEqual(len(notice), 1)
        self.assertIn("dropped", notice[0]["content"])
        self.assertLess(len(sent), 60)


class StoredFormatUnchangedTests(unittest.TestCase):
    """Upgrade safety: a session file written by the PREVIOUS version keeps working, and this
    build never rewrites, normalizes away, or shortens what is already on disk."""

    OLD_FILE = {
        "sessions": [{
            "id": "abc123",
            "owner": "uid-1",
            "title": "old session",
            "created_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-07-01T00:05:00Z",
            "messages": [
                {"role": "user", "content": "old question", "created_at": "2026-07-01T00:00:00Z"},
                {"role": "assistant", "content": "old answer",
                 "created_at": "2026-07-01T00:00:01Z",
                 "tool_trace": [{"tool": "impact", "args": {"repo": "r"}}],
                 "usage": {"calls": [{}], "input_tokens": 5},
                 "feedback": {"vote": "up", "comment": "", "created_at": "2026-07-01T00:01:00Z"}},
            ],
        }],
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "chat_sessions.json")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.OLD_FILE, handle, ensure_ascii=False, indent=2)
        self._patch = mock.patch.object(config, "SESSION_STORE", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_old_file_still_lists_and_loads(self):
        self.assertEqual([s["id"] for s in session_store.list_sessions("uid-1")], ["abc123"])
        detail = session_store.get_session("abc123", "uid-1")
        self.assertEqual(len(detail["messages"]), 2)
        self.assertEqual(detail["messages"][1]["feedback"]["vote"], "up")

    def test_history_for_agent_shape_is_unchanged(self):
        self.assertEqual(session_store.history_for_agent("abc123", "uid-1"),
                         [{"role": "user", "content": "old question"},
                          {"role": "assistant", "content": "old answer"}])

    def test_appending_preserves_every_pre_existing_message_verbatim(self):
        session_store.append_exchange("abc123", "new q", "new a", owner="uid-1")
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        messages = saved["sessions"][0]["messages"]
        self.assertEqual(messages[:2], self.OLD_FILE["sessions"][0]["messages"])
        self.assertEqual(len(messages), 4)

    def test_unknown_fields_written_by_another_version_survive_a_save(self):
        """A field this build does not know about must not be silently dropped on write —
        otherwise upgrading (or rolling back) quietly destroys data."""
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["sessions"][0]["future_field"] = {"kept": True}
        data["schema_version"] = 99
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

        session_store.append_exchange("abc123", "q", "a", owner="uid-1")

        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["sessions"][0]["future_field"], {"kept": True})
        self.assertEqual(saved["schema_version"], 99)

    def test_usage_summary_reads_the_old_file(self):
        summary = session_store.usage_summary()
        self.assertEqual(summary["session_count"], 1)
        self.assertEqual(summary["total"]["answers"], 1)


if __name__ == "__main__":
    unittest.main()
