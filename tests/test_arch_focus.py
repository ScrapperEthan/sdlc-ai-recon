import unittest
from unittest import mock

from retriever import arch_focus, usecase_master

# A small catalog exercising the vendor-vs-channel rule (not the real static/arch_nodes.json).
NODES = [
    {"id": "sms-topics", "role": "topic", "channel": "sms"},
    {"id": "sms-deli", "role": "delivery-job", "channel": "sms"},
    {"id": "sms-csl", "role": "outbound-api", "channel": "sms", "vendor": "csl"},
    {"id": "sms-sinch", "role": "outbound-api", "channel": "sms", "vendor": "sinch"},
    {"id": "ext-sinch", "role": "external", "channel": "sms", "vendor": "sinch"},
    {"id": "ext-csl", "role": "external", "channel": "sms", "vendor": "csl"},
    {"id": "email-deli", "role": "delivery-job", "channel": "email"},
    {"id": "decision-job", "role": "decision"},
    {"id": "decision-topics", "role": "decision"},
]


class AffectedNodesTests(unittest.TestCase):
    def test_vendor_chain_excludes_other_vendors(self):
        hit = arch_focus.affected_nodes(NODES, "vendor", "sinch")
        # Sinch's own outbound + terminal, plus the SHARED upstream (topics + delivery job) + routers
        for node_id in ("sms-sinch", "ext-sinch", "sms-topics", "sms-deli", "decision-job", "decision-topics"):
            self.assertIn(node_id, hit)
        # other vendors' outbound/terminal are NOT pulled in by a single-vendor outage
        self.assertNotIn("sms-csl", hit)
        self.assertNotIn("ext-csl", hit)
        # nor an unrelated channel
        self.assertNotIn("email-deli", hit)

    def test_channel_covers_all_vendors(self):
        hit = arch_focus.affected_nodes(NODES, "channel", "sms")
        for node_id in ("sms-topics", "sms-deli", "sms-csl", "sms-sinch", "ext-sinch", "ext-csl", "decision-job"):
            self.assertIn(node_id, hit)
        self.assertNotIn("email-deli", hit)


class FocusTests(unittest.TestCase):
    def test_focus_real_catalog_vendor(self):
        result = arch_focus.focus("vendor", "sinch")
        self.assertTrue(result["ok"])
        self.assertEqual(result["highlight"], "vendor:sinch")
        self.assertEqual(result["view"], "arch")
        self.assertTrue(result["url"].startswith("/arch.html?embed=1&highlight=vendor:sinch"))
        self.assertIn("ext-sinch", result["affected_node_ids"])
        self.assertNotIn("sms-csl", result["affected_node_ids"])
        self.assertGreater(result["affected_node_count"], 0)

    def test_focus_real_catalog_channel(self):
        result = arch_focus.focus("channel", "sms")
        self.assertTrue(result["ok"])
        # the whole SMS lane is broader than a single vendor
        self.assertGreater(result["affected_node_count"], len(arch_focus.focus("vendor", "sinch")["affected_node_ids"]))

    def test_unknown_value_lists_options(self):
        result = arch_focus.focus("vendor", "nope")
        self.assertFalse(result["ok"])
        self.assertIn("vendors", result)
        self.assertIn("sinch", result["vendors"])

    def test_bad_kind_rejected(self):
        result = arch_focus.focus("repo", "x")
        self.assertFalse(result["ok"])
        self.assertIn("channels", result)
        self.assertIn("vendors", result)


class BusinessSourceFocusTests(unittest.TestCase):
    """arch_focus's business-upstream gutter — seeded in the real static/arch_nodes.json (block 4)."""

    def test_focus_source_system_hits_gutter_node_and_early_spine(self):
        result = arch_focus.focus("source-system", "PEGA")
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "source-system")
        self.assertIn("biz-pega", result["affected_node_ids"])
        self.assertIn("ingress-api", result["affected_node_ids"])
        self.assertIn("decision-topics", result["affected_node_ids"])
        self.assertIn("decision-job", result["affected_node_ids"])
        # arch.html reconstructs the gutter purely client-side from the URL — the properly-cased
        # label must travel with it or a dynamic node would render lower-cased (Round B6).
        self.assertIn("&label=PEGA", result["url"])

    def test_focus_source_system_case_insensitive_and_unknown(self):
        self.assertTrue(arch_focus.focus("source-system", "pega")["ok"])
        result = arch_focus.focus("source-system", "does-not-exist")
        self.assertFalse(result["ok"])
        self.assertIn("source_systems", result)

    def test_focus_use_case_resolves_via_master_snapshot(self):
        with mock.patch.object(usecase_master, "master_for", return_value={"source_system": "PEGA"}):
            result = arch_focus.focus("use-case", "UC123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["value"], "pega")
        self.assertIn("biz-pega", result["affected_node_ids"])
        self.assertIn("resolved from use-case:UC123", result["note"])

    def test_focus_use_case_without_declared_source_system_is_clean_error(self):
        with mock.patch.object(usecase_master, "master_for", return_value=None):
            result = arch_focus.focus("use-case", "UC999")
        self.assertFalse(result["ok"])


class DynamicBusinessSourceTests(unittest.TestCase):
    """Round B6: the gutter is no longer limited to the 5 statically pre-declared source systems —
    any REAL (known, has >=1 use case) canonicalized source_system resolves, synthesized on the fly."""

    _KNOWN = [{"canonical": "powercard", "display_name": "PowerCard", "use_case_count": 107,
               "active_count": 100, "inactive_count": 7}]

    def test_unknown_but_real_source_system_synthesizes_gutter_node(self):
        with mock.patch.object(usecase_master, "source_systems", return_value=self._KNOWN), \
             mock.patch.object(usecase_master, "canonicalize_source_system",
                               return_value={"canonical": "powercard", "display_name": "PowerCard",
                                             "raw": "PowerCard"}), \
             mock.patch.object(usecase_master, "use_cases_for_source_system",
                               return_value={"items": []}):
            result = arch_focus.focus("source-system", "PowerCard")
        self.assertTrue(result["ok"])
        self.assertIn("biz-dyn-powercard", result["affected_node_ids"])
        self.assertIn("ingress-api", result["affected_node_ids"])
        self.assertIn("edge_confidence", result)

    def test_genuinely_unknown_source_system_still_a_clean_error(self):
        with mock.patch.object(usecase_master, "source_systems", return_value=self._KNOWN), \
             mock.patch.object(usecase_master, "canonicalize_source_system",
                               return_value={"canonical": "totallyfake", "display_name": "TotallyFake",
                                             "raw": "TotallyFake"}):
            result = arch_focus.focus("source-system", "TotallyFake")
        self.assertFalse(result["ok"])
        self.assertIn("source_systems", result)
        self.assertIn("PowerCard", result["source_systems"])

    def test_endpoint_evidence_confidence_declared_when_resolved(self):
        with mock.patch.object(usecase_master, "source_systems", return_value=self._KNOWN), \
             mock.patch.object(usecase_master, "canonicalize_source_system",
                               return_value={"canonical": "powercard", "display_name": "PowerCard",
                                             "raw": "PowerCard"}), \
             mock.patch.object(usecase_master, "use_cases_for_source_system", return_value={"items": [
                 {"endpoint_repos": [{"raw": "svc-a", "repo": "svc-a", "confidence": "declared-exact"}]},
                 {"endpoint_repos": [{"raw": "svc-a", "repo": "svc-a", "confidence": "declared-exact"}]},
             ]}):
            result = arch_focus.focus("source-system", "PowerCard")
        self.assertEqual(result["edge_confidence"], "declared-db")
        self.assertIn("svc-a", result["summary"])

    def test_use_case_focus_surfaces_endpoint_repos_and_confidence(self):
        with mock.patch.object(usecase_master, "master_for", return_value={"source_system": "PEGA"}), \
             mock.patch.object(usecase_master, "ext_by_use_case_id", return_value={
                 "uc123": {"endpoint": "mc-hk-hase-pega-adapter-job"}}), \
             mock.patch.object(usecase_master, "resolve_endpoint", return_value=[
                 {"raw": "mc-hk-hase-pega-adapter-job", "repo": "mc-hk-hase-pega-adapter-job",
                  "confidence": "declared-exact"}]):
            result = arch_focus.focus("use-case", "UC123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["edge_confidence"], "declared-db")
        self.assertEqual(result["endpoint_repos"][0]["repo"], "mc-hk-hase-pega-adapter-job")
        self.assertIn("mc-hk-hase-pega-adapter-job", result["summary"])

    def test_use_case_focus_no_endpoint_is_generic_unverified(self):
        with mock.patch.object(usecase_master, "master_for", return_value={"source_system": "PEGA"}), \
             mock.patch.object(usecase_master, "ext_by_use_case_id", return_value={}):
            result = arch_focus.focus("use-case", "UC999")
        self.assertTrue(result["ok"])
        self.assertEqual(result["edge_confidence"], "generic-unverified")
        self.assertNotIn("endpoint_repos", result)


if __name__ == "__main__":
    unittest.main()
