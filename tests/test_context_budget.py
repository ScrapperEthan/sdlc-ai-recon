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
            text = context_budget.shrink_tool_result(result, max_tokens=600)
            json.loads(text)                       # must not raise
            self.assertLessEqual(context_budget.estimate_tokens(text), 600)

    def test_long_string_is_capped_with_an_explicit_marker(self):
        out = json.loads(context_budget.shrink_tool_result(
            {"ok": True, "log": "x" * 50000}, max_tokens=1200, string_cap=500))
        self.assertTrue(out["log"].endswith("chars total]"))
        self.assertIn("50000", out["log"])
        self.assertTrue(any(n["kind"] == "string" for n in out["_truncated"]))

    def test_long_list_is_shortened_and_the_loss_is_recorded(self):
        out = json.loads(context_budget.shrink_tool_result(
            {"ok": True, "use_cases": [{"id": f"C{i}"} for i in range(2000)]}, max_tokens=900))
        self.assertLess(len(out["use_cases"]), 2000)
        note = next(n for n in out["_truncated"] if n["kind"] == "list")
        self.assertEqual(note["total"], 2000)
        self.assertEqual(note["kept"], len(out["use_cases"]))
        self.assertIn("use_cases", note["path"])

    def test_indivisible_payload_falls_back_to_a_self_describing_envelope(self):
        out = json.loads(context_budget.shrink_tool_result({"blob": "q" * 200000}, max_tokens=400,
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
        json.loads(context_budget.shrink_tool_result(result, max_tokens=400))

    def test_top_level_list_is_wrapped_rather_than_losing_the_truncation_note(self):
        out = json.loads(context_budget.shrink_tool_result(
            [{"id": i, "pad": "p" * 200} for i in range(500)], max_tokens=600))
        self.assertIn("_result", out)
        self.assertTrue(out["_truncated"])

    def test_unserializable_result_does_not_raise(self):
        out = json.loads(context_budget.shrink_tool_result({"fn": object()}, max_tokens=300))
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
        kept, dropped = context_budget.fit_history(history, budget=800)   # tokens, not chars
        self.assertGreater(dropped, 0)
        self.assertEqual(kept[-1], history[-1])
        self.assertLessEqual(
            sum(context_budget.estimate_tokens(m["content"]) + 8 for m in kept), 800)

    def test_the_opening_question_is_always_kept(self):
        history = self._history(40, size=200)
        kept, _ = context_budget.fit_history(history, budget=800)
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
        with mock.patch.object(config, "CONTEXT_TOKENS", 20000):
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


class BudgetLaneTests(unittest.TestCase):
    """The point of a budget: the lanes are carved out of ONE total, and the total holds.

    Independent caps could not do this — a history cap and a per-tool-call cap know nothing about
    each other, so their worst cases simply added up with nothing watching the sum.
    """

    def _budget(self, total=100000, system="short prompt"):
        budget = context_budget.Budget(total=total, output_reserve=4096)
        budget.reserve_system(system)
        return budget

    def test_lanes_never_exceed_the_working_budget(self):
        budget = self._budget()
        self.assertLessEqual(sum(budget.lanes.values()), budget.working)

    def test_system_prompt_and_output_reserve_come_off_the_top(self):
        budget = context_budget.Budget(total=100000, output_reserve=4096)
        budget.reserve_system("x" * 36000)                     # ~10k tokens
        self.assertGreater(budget.system_tokens, 9000)
        self.assertEqual(budget.working,
                         100000 - budget.system_tokens - 4096)
        self.assertLess(budget.working, 100000 - 4096)

    def test_the_tools_lane_is_cumulative_not_per_call(self):
        """Eight greedy calls used to cost 8x the per-call cap. Now they share one lane."""
        budget = self._budget()
        lane = budget.lanes["tools"]
        fat = {"rows": [{"i": i, "pad": "p" * 400} for i in range(4000)]}
        for call in range(8):
            budget.fit_tool_result(fat, calls_remaining=8 - call)
        self.assertLessEqual(budget.spent["tools"], lane * 1.1)   # 10% slack for the min floor

    def test_allowance_shrinks_as_the_lane_is_consumed(self):
        budget = self._budget()
        first = budget.tool_allowance(calls_remaining=4)
        budget.charge("tools", "z" * (budget.lanes["tools"] * 3))  # burn most of the lane
        self.assertLess(budget.tool_allowance(calls_remaining=4), first)

    def test_a_greedy_first_call_cannot_starve_the_rest_of_the_turn(self):
        budget = self._budget()
        huge = {"rows": [{"pad": "p" * 800} for _ in range(9000)]}
        budget.fit_tool_result(huge, calls_remaining=8)
        self.assertGreater(budget.remaining("tools"), budget.lanes["tools"] * 0.5)

    def test_history_is_bounded_by_rounds_as_well_as_tokens(self):
        budget = self._budget()
        history = []
        for i in range(40):
            history.append({"role": "user", "content": f"q{i}"})
            history.append({"role": "assistant", "content": f"a{i}"})
        kept, dropped = budget.fit_history(history, max_rounds=10)
        self.assertLessEqual(len(kept), 10 * 2 + 1)     # +1 for the retained opening question
        self.assertGreater(dropped, 0)
        self.assertEqual(kept[-1], history[-1])

    def test_subagent_lane_is_reserved_even_though_nothing_fills_it_yet(self):
        budget = self._budget()
        self.assertGreater(budget.lanes["subagent"], 0)
        self.assertEqual(budget.spent["subagent"], 0)

    def test_compaction_lane_is_reserved_for_the_summarizer_we_have_not_built(self):
        budget = self._budget()
        self.assertGreater(budget.lanes["compaction"], 0)

    def test_report_says_the_numbers_are_estimates(self):
        report = self._budget().report()
        self.assertTrue(report["estimated"])
        self.assertIn("ESTIMATES", report["note"])
        self.assertEqual(set(report["lanes"]), set(config.CONTEXT_LANE_SHARES))

    def test_whole_turn_stays_under_the_total(self):
        """The end-to-end guarantee: history + every tool result + the prompt still leaves room
        for the reserved output."""
        budget = self._budget(total=60000)
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "文字" * 3000}
                   for i in range(60)]
        kept, _ = budget.fit_history(history)
        fat = {"rows": [{"pad": "p" * 500} for _ in range(5000)]}
        for call in range(8):
            budget.fit_tool_result(fat, calls_remaining=8 - call)
        used = budget.system_tokens + sum(budget.spent.values())
        self.assertLessEqual(used, budget.total - budget.output_reserve + 500)
        self.assertTrue(kept)


class TokenEstimateTests(unittest.TestCase):
    def test_cjk_costs_about_one_token_per_character(self):
        tokens = context_budget.estimate_tokens("消息平台事故根因分析" * 10)
        self.assertGreater(tokens, 90)
        self.assertLess(tokens, 140)

    def test_latin_costs_far_fewer_tokens_per_character(self):
        latin = context_budget.estimate_tokens("a" * 100)
        cjk = context_budget.estimate_tokens("文" * 100)
        self.assertLess(latin, cjk / 2)

    def test_estimate_errs_high_not_low(self):
        """Under-estimating costs a failed request; over-estimating costs a shorter answer."""
        self.assertGreater(context_budget.estimate_tokens("a" * 360), 360 / 3.6)

    def test_empty_and_non_string_are_safe(self):
        self.assertEqual(context_budget.estimate_tokens(""), 0)
        self.assertEqual(context_budget.estimate_tokens(None), 0)
        self.assertGreater(context_budget.estimate_tokens(12345), 0)


class ToolAllowanceCeilingTests(unittest.TestCase):
    """Regression for the bug the owner's question surfaced: tool_allowance used to return
    _MIN_TOOL_TOKENS even when the lane had far less than that left, letting the cumulative spend
    quietly overshoot the budget by up to _MIN_TOOL_TOKENS per remaining call."""

    def _budget(self, total=100000):
        budget = context_budget.Budget(total=total, output_reserve=4096)
        budget.reserve_system("short prompt")
        return budget

    def test_allowance_never_exceeds_what_is_actually_left_in_the_lane(self):
        budget = self._budget()
        # Burn the lane down to a sliver well under _MIN_TOOL_TOKENS.
        budget.charge("tools", "z" * (budget.lanes["tools"] * 4 - 40))
        sliver = budget.remaining("tools")
        self.assertLess(sliver, context_budget._MIN_TOOL_TOKENS)
        self.assertLessEqual(budget.tool_allowance(calls_remaining=3), sliver)

    def test_exhausted_lane_returns_zero_not_the_old_floor(self):
        budget = self._budget()
        budget.charge("tools", "z" * (budget.lanes["tools"] * 4))
        self.assertEqual(budget.remaining("tools"), 0)
        self.assertEqual(budget.tool_allowance(calls_remaining=5), 0)

    def test_eight_greedy_calls_near_exhaustion_do_not_overshoot_the_lane(self):
        """The exact scenario: MAX_TOOL_ITERS=8 calls, each trying to return something huge, with
        the lane already almost spent before the last few calls run."""
        budget = self._budget()
        fat = {"rows": [{"pad": "p" * 900} for _ in range(6000)]}
        budget.charge("tools", "z" * int(budget.lanes["tools"] * 0.97))   # pre-exhaust the lane
        for call in range(8):
            budget.fit_tool_result(fat, calls_remaining=8 - call)
        # Old buggy code could overshoot by up to _MIN_TOOL_TOKENS per remaining call (~1600
        # tokens here). Fixed code should track the lane almost exactly, plus only the small fixed
        # envelope overhead each fallback preview costs.
        overshoot = budget.spent["tools"] - budget.lanes["tools"]
        self.assertLess(overshoot, 400, f"tools lane overshot by {overshoot} tokens")
