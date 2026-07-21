import unittest
from unittest import mock

from webapp import tools


class SourceSystemToolTests(unittest.TestCase):
    def test_missing_source_system_is_clean_error(self):
        result = tools.dispatch("source_system_impact", {})
        self.assertFalse(result["ok"])
        self.assertIn("source_system", result["error"])

    def test_unknown_source_system_is_clean_error_not_a_crash(self):
        with mock.patch.object(tools.usecase_master, "use_cases_for_source_system",
                               return_value={"available": True, "items": []}):
            result = tools.dispatch("source_system_impact", {"source_system": "NOPE"})
        self.assertFalse(result["ok"])
        self.assertIn("unknown target", result["error"])

    def test_delegates_to_impact_report_build_report(self):
        sentinel = {"target": {"kind": "source-system"}}
        with mock.patch.object(tools.impact_report, "build_report", return_value=sentinel) as build:
            result = tools.dispatch("source_system_impact", {"source_system": "PEGA"})
        # Default pagination (top-50 + include_inactive=False) so MDC-scale sources don't overflow
        # the LLM context by default (B8) — the funnel counts stay full regardless.
        build.assert_called_once_with(
            "source-system:PEGA", include_inactive=False, offset=0, limit=50)
        self.assertEqual(result, sentinel)

    def test_pagination_args_pass_through(self):
        sentinel = {"target": {"kind": "source-system"}}
        with mock.patch.object(tools.impact_report, "build_report", return_value=sentinel) as build:
            tools.dispatch("source_system_impact", {
                "source_system": "MDC", "include_inactive": True, "offset": 50, "limit": 25,
            })
        build.assert_called_once_with(
            "source-system:MDC", include_inactive=True, offset=50, limit=25)

    def test_list_source_systems_delegates(self):
        with mock.patch.object(tools.usecase_master, "source_systems", return_value=[{"source_system": "PEGA"}]):
            result = tools.dispatch("list_source_systems", {})
        self.assertEqual(result, {"items": [{"source_system": "PEGA"}]})


if __name__ == "__main__":
    unittest.main()
