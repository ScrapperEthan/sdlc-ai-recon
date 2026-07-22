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

    def test_list_repos_delegates_to_repo_tags_filter_repos(self):
        sentinel = {"filters": {"query": "mdc"}, "count": 1, "repos": ["amet-mdc-hsbc-batch-email-job"]}
        with mock.patch.object(tools.repo_tags, "filter_repos", return_value=sentinel) as filt:
            result = tools.dispatch("list_repos", {"query": "mdc", "mode": "api"})
        filt.assert_called_once_with(channel=None, mode="api", system=None, query="mdc", mdc_common=None)
        self.assertEqual(result, sentinel)

    def test_list_repos_missing_repo_tags_json_is_clean_error_not_a_crash(self):
        with mock.patch.object(tools.repo_tags, "filter_repos", side_effect=FileNotFoundError("no repo_tags.json")):
            result = tools.dispatch("list_repos", {"query": "mdc"})
        self.assertFalse(result["ok"])
        self.assertIn("repo_tags.json", result["error"])

    def test_list_repos_group_mdc_delegates_to_mdc_repos(self):
        # group="mdc" bypasses filter_repos entirely — it needs the amet-mdc-prefix UNION mdc_common
        # union that plain query/system filtering can't express (mc-hk-hase-* repos have neither).
        sentinel = {"group": "mdc", "count": 2, "repos": [
            {"repo": "amet-mdc-hsbc-batch-email-job", "via": ["amet-mdc-prefix"]},
            {"repo": "mc-hk-hase-sms-job", "via": ["mdc_common"]},
        ], "by_source": {"amet-mdc": 1, "mdc_common": 1}}
        with mock.patch.object(tools.repo_tags, "mdc_repos", return_value=sentinel) as mdc, \
                mock.patch.object(tools.repo_tags, "filter_repos") as filt:
            result = tools.dispatch("list_repos", {"group": "mdc"})
        mdc.assert_called_once_with()
        filt.assert_not_called()
        self.assertEqual(result, sentinel)

    def test_list_repos_group_mdc_missing_repo_tags_json_is_clean_error_not_a_crash(self):
        with mock.patch.object(tools.repo_tags, "mdc_repos", side_effect=FileNotFoundError("no repo_tags.json")):
            result = tools.dispatch("list_repos", {"group": "MDC"})
        self.assertFalse(result["ok"])
        self.assertIn("repo_tags.json", result["error"])

    def test_usecase_impact_missing_id_is_clean_error(self):
        result = tools.dispatch("usecase_impact", {})
        self.assertFalse(result["ok"])
        self.assertIn("use_case_id", result["error"])

    def test_usecase_impact_delegates_to_impact_report_build_report(self):
        sentinel = {"target": {"kind": "use-case"}}
        with mock.patch.object(tools.impact_report, "build_report", return_value=sentinel) as build:
            result = tools.dispatch("usecase_impact", {"use_case_id": "M2050"})
        build.assert_called_once_with("use-case:M2050")
        self.assertEqual(result, sentinel)

    def test_usecase_impact_unknown_id_is_clean_error_not_a_crash(self):
        with mock.patch.object(tools.impact_report, "build_report",
                               side_effect=FileNotFoundError("unknown target: use-case:NOPE")):
            result = tools.dispatch("usecase_impact", {"use_case_id": "NOPE"})
        self.assertFalse(result["ok"])
        self.assertIn("unknown target", result["error"])

    def test_search_usecases_delegates_with_defaults(self):
        sentinel = {"available": True, "items": []}
        with mock.patch.object(tools.usecase_master, "search_usecases", return_value=sentinel) as search:
            result = tools.dispatch("search_usecases", {"query": "alert"})
        search.assert_called_once_with(
            query="alert", source_system=None, include_inactive=False, channel=None,
            business_category_code=None, country=None, service_line=None, delivery_mode=None,
            offset=0, limit=50)
        self.assertEqual(result, sentinel)

    def test_search_usecases_filters_pass_through(self):
        sentinel = {"available": True, "items": []}
        with mock.patch.object(tools.usecase_master, "search_usecases", return_value=sentinel) as search:
            tools.dispatch("search_usecases", {
                "source_system": "PEGA", "include_inactive": True, "channel": "SMS",
                "business_category_code": "11", "country": "HK", "service_line": "1",
                "delivery_mode": "REALTIME", "offset": 10, "limit": 20,
            })
        search.assert_called_once_with(
            query=None, source_system="PEGA", include_inactive=True, channel="SMS",
            business_category_code="11", country="HK", service_line="1", delivery_mode="REALTIME",
            offset=10, limit=20)

    def test_usecase_quality_findings_delegates_with_defaults(self):
        sentinel = {"available": True, "findings": []}
        with mock.patch.object(tools.usecase_consistency, "quality_findings",
                               return_value=sentinel) as qf:
            result = tools.dispatch("usecase_quality_findings", {})
        qf.assert_called_once_with(severity=None, offset=0, limit=50)
        self.assertEqual(result, sentinel)

    def test_usecase_quality_findings_severity_pass_through(self):
        sentinel = {"available": True, "findings": []}
        with mock.patch.object(tools.usecase_consistency, "quality_findings",
                               return_value=sentinel) as qf:
            tools.dispatch("usecase_quality_findings", {"severity": "error", "offset": 5, "limit": 10})
        qf.assert_called_once_with(severity="error", offset=5, limit=10)


if __name__ == "__main__":
    unittest.main()
