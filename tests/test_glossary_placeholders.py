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


if __name__ == "__main__":
    unittest.main()
