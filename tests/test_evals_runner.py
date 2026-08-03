"""The eval runner's own assertions.

A regression harness has to be trustworthy before its green means anything: an assertion that
silently never fires turns "20/20 PASS" into a lie that is harder to catch than no eval at all. So
the scoring logic is tested against synthetic answers — no model, no mirror, fast.

The cases file is checked too: every case must carry a `why` naming the RUNBOOK or owner decision
it encodes. A red case nobody can trace back to a real defect gets deleted rather than fixed.
"""
import json
import os
import re
import unittest

from evals import run as evals


def _result(answer="", tools=(), citations=None):
    return {"answer": answer,
            "tool_trace": [{"tool": name} for name in tools],
            "citations": citations}


def _cite(*refs_ok):
    return {"items": [{"ref": ref, "ok": ok} for ref, ok in refs_ok],
            "verified": sum(1 for _r, ok in refs_ok if ok), "total": len(refs_ok)}


def _verdict(case, result):
    checks = evals._evaluate(case, result)
    return all(item["ok"] for item in checks), checks


class MustNotMentionTests(unittest.TestCase):
    """The load-bearing check: the sentence that would be a lie if the lookup never happened."""

    CASE = {"id": "x", "must_not_mention": ["未发现异常", "no anomaly"]}

    def test_fails_when_the_forbidden_sentence_appears(self):
        ok, checks = _verdict(self.CASE, _result("查询完成，未发现异常。"))
        self.assertFalse(ok)
        self.assertEqual([c["detail"] for c in checks if not c["ok"]], ["SAID IT"])

    def test_is_case_insensitive(self):
        self.assertFalse(_verdict(self.CASE, _result("Found NO ANOMALY in the window."))[0])

    def test_passes_when_absent(self):
        self.assertTrue(_verdict(self.CASE, _result("时区缺失，没有发起任何查询。"))[0])


class AskBackTests(unittest.TestCase):
    CASE = {"id": "x", "must_ask_back": True}

    def test_a_question_counts_as_asking(self):
        self.assertTrue(_verdict(self.CASE, _result("这个 03:15 是 HKT 还是 UTC？"))[0])

    def test_a_refusal_without_a_question_mark_still_counts(self):
        self.assertTrue(_verdict(self.CASE, _result("缺少时区，无法确定查询窗口。"))[0])

    def test_a_confident_answer_fails(self):
        ok, checks = _verdict(self.CASE, _result("日志里有 12 条 SocketTimeout。"))
        self.assertFalse(ok)
        self.assertIn("answered instead of asking",
                      [c["detail"] for c in checks if not c["ok"]])


class ToolTraceTests(unittest.TestCase):
    def test_must_not_call_catches_the_refused_then_called_defect(self):
        """RUNBOOK-61: the plan refused for a missing timezone and the tool ran anyway."""
        case = {"id": "x", "must_not_call_tools": ["incident_investigate"]}
        ok, checks = _verdict(case, _result("缺时区", tools=["incident_investigate"]))
        self.assertFalse(ok)
        self.assertIn("CALLED IT", [c["detail"] for c in checks if not c["ok"]])
        self.assertTrue(_verdict(case, _result("缺时区", tools=["impact"]))[0])

    def test_any_of_several_tools_satisfies_tool_any(self):
        case = {"id": "x", "must_call_tools_any": ["message_flow", "unified_impact"]}
        self.assertTrue(_verdict(case, _result("a", tools=["unified_impact"]))[0])
        self.assertFalse(_verdict(case, _result("a", tools=["hubs"]))[0])

    def test_tool_budget_bounds_a_fan_out(self):
        case = {"id": "x", "max_tool_calls": 2}
        self.assertTrue(_verdict(case, _result("a", tools=["a", "b"]))[0])
        self.assertFalse(_verdict(case, _result("a", tools=["a", "b", "c"]))[0])


class CitationTests(unittest.TestCase):
    def test_an_unverified_citation_fails_the_case(self):
        case = {"id": "x", "citations_must_verify": True}
        good = _result("a", citations=_cite(("repo/A.java:10", True)))
        bad = _result("a", citations=_cite(("repo/A.java:10", True), ("repo/Made.java:9", False)))
        self.assertTrue(_verdict(case, good)[0])
        self.assertFalse(_verdict(case, bad)[0])

    def test_min_verified_counts_only_the_verified_ones(self):
        case = {"id": "x", "min_verified_citations": 2}
        one_real = _result("a", citations=_cite(("a/A.java:1", True), ("b/B.java:2", False)))
        self.assertFalse(_verdict(case, one_real)[0])

    def test_a_file_only_citation_is_not_finished_work(self):
        case = {"id": "x", "citations_need_line_numbers": True}
        self.assertTrue(_verdict(case, _result("a", citations=_cite(("a/A.java:12", True))))[0])
        self.assertFalse(_verdict(case, _result("a", citations=_cite(("a/A.java", True))))[0])

    def test_it_falls_back_to_verifying_the_text_when_the_agent_sent_no_report(self):
        """Must not silently score 0 unverified just because the key was absent."""
        case = {"id": "x", "citations_must_verify": True}
        checks = evals._evaluate(case, {"answer": "see repo/A.java:10", "tool_trace": []})
        self.assertTrue(any(c["check"] == "cite-verify" for c in checks))


class MentionTests(unittest.TestCase):
    def test_say_any_needs_only_one_of_the_alternatives(self):
        case = {"id": "x", "must_mention_any": ["快照", "snapshot"]}
        self.assertTrue(_verdict(case, _result("这是 UAT 快照"))[0])
        self.assertTrue(_verdict(case, _result("from the snapshot"))[0])
        self.assertFalse(_verdict(case, _result("生产上就是这样"))[0])

    def test_say_requires_every_listed_phrase(self):
        case = {"id": "x", "must_mention": ["45", "460"]}
        self.assertFalse(_verdict(case, _result("45 个"))[0])
        self.assertTrue(_verdict(case, _result("45 个，全库 460"))[0])


class RunnerBehaviourTests(unittest.TestCase):
    def test_a_crashing_case_is_recorded_not_raised(self):
        """One dead case must not cost the other nineteen their run."""
        def _boom(_question):
            raise RuntimeError("model unreachable")

        original = evals._answer_in_process
        evals._answer_in_process = _boom
        try:
            result = evals._run_case({"id": "c", "question": "q"})
        finally:
            evals._answer_in_process = original
        self.assertEqual(result["passed"], 0)
        self.assertIn("model unreachable", result["error"])

    def test_delta_names_a_regression_explicitly(self):
        previous = {"c": {"passed": 3, "total": 3}}
        self.assertEqual(evals._delta({"id": "c", "passed": 2, "total": 3}, previous),
                         "DOWN was PASS")
        self.assertEqual(evals._delta({"id": "c", "passed": 3, "total": 3}, previous), "=")
        self.assertEqual(evals._delta({"id": "new", "passed": 1, "total": 1}, previous), "new")


class PlaceholderTests(unittest.TestCase):
    """RUNBOOK-65: the external side wrote `mc-hk-hase-csl-sms-deli-job` — not a real repo — into a
    runbook AND into these cases. The system fail-closed correctly, but a case built on a repo
    nobody has heard of measures nothing, and a BASELINE built on one is worse than no baseline.
    Real ids now come from the intranet-owned config; unresolved means SKIP, never guess."""

    def test_a_resolved_placeholder_is_substituted(self):
        case = {"id": "x", "question": "{sms_delivery} 挂了会影响谁？"}
        resolved, missing = evals.resolve_case(case, {"sms_delivery": "mc-hk-hase-real-one"})
        self.assertEqual(missing, [])
        self.assertEqual(resolved["question"], "mc-hk-hase-real-one 挂了会影响谁？")

    def test_an_unfilled_placeholder_is_reported_missing_not_substituted(self):
        case = {"id": "x", "question": "{sms_delivery} 挂了会影响谁？"}
        _resolved, missing = evals.resolve_case(case, {"sms_delivery": ""})
        self.assertEqual(missing, ["sms_delivery"])

    def test_a_completely_absent_config_misses_every_placeholder(self):
        case = {"id": "x", "question": "{a} and {b}"}
        _resolved, missing = evals.resolve_case(case, {})
        self.assertEqual(missing, ["a", "b"])

    def test_a_case_with_no_placeholder_always_runs(self):
        case = {"id": "x", "question": "MDC 有多少个仓库？"}
        resolved, missing = evals.resolve_case(case, {})
        self.assertEqual(missing, [])
        self.assertEqual(resolved["question"], case["question"])

    def test_the_readme_block_is_not_treated_as_a_placeholder_value(self):
        repos = evals.load_repos(evals.DEFAULT_REPOS)
        self.assertNotIn("_README", repos)

    def test_the_shipped_config_leaves_the_intranet_owned_ids_blank(self):
        """A committed guess would defeat the whole point — blank is the correct shipped state."""
        with open(evals.DEFAULT_REPOS, encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        for key in ("sms_delivery", "vendor_repo", "known_use_case"):
            self.assertEqual(raw[key], "", key)

    def test_no_case_hardcodes_an_unverified_repo_id(self):
        """The one allowed literal is the deliberately-fake repo, whose whole point is to be fake."""
        allowed = {"mc-hk-hase-totally-made-up-service"}
        for case in evals._load_cases(evals.DEFAULT_CASES):
            for token in re.findall(r"mc-hk-hase-[a-z0-9-]+", case["question"]):
                with self.subTest(case=case["id"]):
                    self.assertIn(token, allowed, f"{case['id']} hardcodes {token}")


class CasesFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = evals._load_cases(evals.DEFAULT_CASES)

    def test_the_file_parses_and_is_not_trivial(self):
        self.assertGreaterEqual(len(self.cases), 15)

    def test_ids_are_unique(self):
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_case_says_why_it_exists(self):
        """A case nobody can trace to a real defect gets deleted, not debugged."""
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(case.get("why"), case["id"])
                self.assertGreater(len(case["why"]), 40, case["id"])

    def test_every_case_asserts_something(self):
        keys = {"must_mention", "must_mention_any", "must_not_mention", "must_ask_back",
                "must_call_tools", "must_call_tools_any", "must_not_call_tools",
                "max_tool_calls", "citations_must_verify", "min_verified_citations",
                "max_unverified_citations", "citations_need_line_numbers", "must_cite_globs",
                "must_flag_partial"}
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(keys & set(case), case["id"])

    def test_both_lanes_are_covered(self):
        lanes = {case.get("lane") for case in self.cases}
        self.assertIn("incident", lanes)
        self.assertIn("retrieval", lanes)

    def test_the_honesty_lane_is_the_biggest_group(self):
        """Every shipped defect had the same shape — say-it-without-looking. Keep the weight there."""
        negative = [c for c in self.cases if c.get("must_not_mention") or c.get("must_ask_back")]
        self.assertGreaterEqual(len(negative), len(self.cases) // 3)

    def test_every_case_is_valid_json_on_one_line(self):
        with open(evals.DEFAULT_CASES, encoding="utf-8-sig") as handle:
            for number, line in enumerate(handle, 1):
                if line.strip():
                    with self.subTest(line=number):
                        json.loads(line)


if __name__ == "__main__":
    unittest.main()
