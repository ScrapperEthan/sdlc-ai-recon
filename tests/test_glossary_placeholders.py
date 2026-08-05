"""The glossary must never dress an unfilled entry up as a decoding.

Observed live 2026-08-05: the box's hand-authored index/glossary.json carried `TBC ? ???????` for
`mc`, `api` and `common`, and the impact view printed

    mc-hk-hase-api-common (mc=TBC ? ???????, hk=HongKong, hase=HASESeng Bank, …)

which is indistinguishable from a real decoding. Same failure shape as reporting a log tail as a
keyword hit. These tests pin that placeholders are dropped, that an undecodable file degrades
instead of taking down every impact report, and that the coverage report tells the box which tokens
are actually worth filling.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import glossary


def _write(path, payload, encoding="utf-8"):
    with open(path, "w", encoding=encoding) as handle:
        json.dump(payload, handle, ensure_ascii=False)


class UnfilledDetectionTest(unittest.TestCase):
    def test_the_observed_value_is_unfilled(self):
        self.assertTrue(glossary.is_unfilled("TBC ? ???????"))

    def test_placeholder_words_are_unfilled(self):
        for value in ("TBC", "tbc", "  TBD ", "n/a", "unknown", "-", "?", "待确认", "TODO", ""):
            self.assertTrue(glossary.is_unfilled(value), value)

    def test_question_mark_run_is_unfilled_whichever_width(self):
        # A non-Unicode save replaces CJK with '?'; the full-width form shows up when the file went
        # through a different code page on the way.
        self.assertTrue(glossary.is_unfilled("???????"))
        self.assertTrue(glossary.is_unfilled("？？？？"))

    def test_real_meanings_survive_including_ones_containing_a_question_mark(self):
        # A single '?' is a human annotation, not corruption — dropping it would lose real content.
        for value in ("HongKong", "Hang Seng Bank", "settlement? see RUNBOOK-49", "批量作业"):
            self.assertFalse(glossary.is_unfilled(value), value)


class ExpandTest(unittest.TestCase):
    def test_placeholder_tokens_are_omitted_not_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            _write(path, {"mc": "TBC ? ???????", "hk": "HongKong",
                          "api": "TBC ? ???????", "common": "TBC ? ???????"})
            out = glossary.expand("mc-hk-hase-api-common", path=path)
        # The real regression: no '?' salad anywhere, and hk still decodes.
        self.assertEqual(out, "mc-hk-hase-api-common (hk=HongKong)")
        self.assertNotIn("?", out)

    def test_every_token_placeholder_leaves_the_name_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            _write(path, {"mc": "TBC", "api": "???????"})
            self.assertEqual(glossary.expand("mc-api", path=path), "mc-api")

    def test_undecodable_file_degrades_instead_of_crashing_every_report(self):
        # A GBK save on a Chinese-Windows box used to raise UnicodeDecodeError straight through
        # impact_report.build_repo_report — one Notepad save took out every impact answer.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            with open(path, "w", encoding="gbk") as handle:
                json.dump({"mc": "信息中心"}, handle, ensure_ascii=False)
            self.assertEqual(glossary.load(path=path), {})
            self.assertEqual(glossary.expand("mc-hk", path=path), "mc-hk")

    def test_malformed_json_still_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(glossary.expand("mc-hk", path=path), "mc-hk")


class CoverageTest(unittest.TestCase):
    NAMES = ["mc-hk-hase-api-common", "mc-hk-hase-api-parent", "amet-mdc-hsbc-cm-outbound-api"]

    def _report(self, authored, names=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            _write(path, authored)
            return glossary.coverage(names if names is not None else self.NAMES, path=path)

    def test_placeholder_is_its_own_state_distinct_from_missing(self):
        report = self._report({"mc": "TBC ? ???????", "hk": "HongKong"})
        states = {item["token"]: item["state"] for item in report["tokens"]}
        self.assertEqual(states["mc"], "placeholder")   # authored, says nothing
        self.assertEqual(states["hk"], "filled")
        self.assertEqual(states["common"], "missing")   # never authored at all

    def test_tokens_are_ranked_by_how_many_names_they_appear_in(self):
        report = self._report({})
        counts = {item["token"]: item["repos"] for item in report["tokens"]}
        self.assertEqual(counts["api"], 3)   # in all three names
        self.assertEqual(counts["mc"], 2)
        self.assertEqual(counts["parent"], 1)
        ranks = [item["repos"] for item in report["tokens"]]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_a_token_is_counted_once_per_name_not_once_per_occurrence(self):
        report = self._report({}, names=["api-api-api"])
        counts = {item["token"]: item["repos"] for item in report["tokens"]}
        self.assertEqual(counts["api"], 1)

    def test_unreadable_file_is_reported_distinctly_from_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            with open(path, "w", encoding="gbk") as handle:
                json.dump({"mc": "信息中心"}, handle, ensure_ascii=False)
            report = glossary.coverage(self.NAMES, path=path)
        self.assertFalse(report["file_readable"])
        markdown = glossary.render_coverage_markdown(report)
        self.assertIn("UTF-8", markdown)

    def test_markdown_lists_only_the_unfilled_tokens(self):
        report = self._report({"mc": "TBC", "hk": "HongKong"})
        markdown = glossary.render_coverage_markdown(report)
        self.assertIn("`mc`", markdown)        # placeholder -> needs filling
        self.assertIn("`common`", markdown)    # missing -> needs filling
        self.assertNotIn("| `hk` |", markdown)  # already filled -> not on the worklist

    def test_write_coverage_emits_both_artefacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            _write(path, {"hk": "HongKong"})
            index_dir = os.path.join(tmp, "index")
            glossary.write_coverage(index_dir, self.NAMES, path=path)
            for name in ("GLOSSARY_COVERAGE.md", "GLOSSARY_COVERAGE.json"):
                self.assertTrue(os.path.exists(os.path.join(index_dir, "reports", name)), name)

    def test_defaults_to_the_repo_tags_estate_when_no_names_given(self):
        with mock.patch.object(glossary, "_repo_names", return_value=["mc-hk"]) as names:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "glossary.json")
                _write(path, {})
                report = glossary.coverage(path=path)
        names.assert_called_once_with(None)
        self.assertEqual(report["repos_measured"], 1)



class CoverageMetricTest(unittest.TestCase):
    """The metric the intranet's real run proved useless, and the two that replace it.

    RUNBOOK-75 measured: the box filled the top 20 ranked tokens (filled 23 -> 43) and
    `repos_with_any_meaning` did not move at all — 459 of 460 before, 459 of 460 after. Almost
    every repo name shares a common token, so "at least one" saturates before any real work starts.
    Quoting it as progress would have reported a full day's filling as zero improvement.
    """

    def _report(self, authored, names):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "glossary.json")
            _write(path, authored)
            return glossary.coverage(names, path=path)

    NAMES = ["mc-hk-api", "mc-hk-job"]

    def test_any_meaning_saturates_and_hides_real_progress(self):
        # One shared token already lights up every name; filling more moves this metric zero.
        before = self._report({"mc": "MDC"}, self.NAMES)
        after = self._report({"mc": "MDC", "hk": "HongKong", "api": "service"}, self.NAMES)
        self.assertEqual(before["repos_with_any_meaning"], after["repos_with_any_meaning"])

    def test_token_slots_decoded_moves_with_every_token_filled(self):
        before = self._report({"mc": "MDC"}, self.NAMES)
        after = self._report({"mc": "MDC", "hk": "HongKong"}, self.NAMES)
        self.assertEqual(before["token_slots"], 6)          # 3 tokens x 2 names
        self.assertEqual(before["token_slots_decoded"], 2)  # 'mc' in both
        self.assertEqual(after["token_slots_decoded"], 4)   # + 'hk' in both

    def test_fully_decoded_needs_every_token_in_the_name(self):
        partial = self._report({"mc": "MDC", "hk": "HongKong"}, self.NAMES)
        self.assertEqual(partial["repos_fully_decoded"], 0)  # 'api'/'job' still unexplained
        complete = self._report(
            {"mc": "MDC", "hk": "HongKong", "api": "service", "job": "scheduled job"}, self.NAMES)
        self.assertEqual(complete["repos_fully_decoded"], 2)

    def test_placeholders_do_not_count_toward_any_completion_metric(self):
        report = self._report({"mc": "MDC", "hk": "TBC ? ???????"}, self.NAMES)
        self.assertEqual(report["token_slots_decoded"], 2)   # 'hk' is authored but says nothing
        self.assertEqual(report["repos_fully_decoded"], 0)

    def test_markdown_leads_with_the_metric_that_moves(self):
        markdown = glossary.render_coverage_markdown(self._report({"mc": "MDC"}, self.NAMES))
        self.assertIn("token slots decoded", markdown)
        self.assertIn("do not read this one as progress", markdown)


if __name__ == "__main__":
    unittest.main()
