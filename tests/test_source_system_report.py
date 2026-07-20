import contextlib
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

import impact_report
import outage_report
from retriever import config as rconfig

MASTER_HEADER = (
    "use_case_id,use_case_name,project_name,source_system,work_stream_name,line_of_business,"
    "business_category,country_code,group_member,app_name,created_by,created_time,modified_by,"
    "last_modified_time,status,marketing_optin_flag\n"
)
# UC001 has a route (via ROUTING/MESSAGES below) -> routed. UC002 shares the same source_system
# but has no route -> catalog-only. UC900 belongs to a different source_system entirely.
MASTER_ROWS = [
    "UC001,Alpha Case,ProjA,PEGA,streamA,WPB,11,HK,HASE,appA,alice,2020-01-01,alice,2020-01-01,Y,Y\n",
    "UC002,Beta Case,ProjB,PEGA,streamB,CMB,6,HK,HSBC,appB,bob,2024-06-01,bob,2024-06-01,Y,N\n",
    "UC900,Other Case,ProjZ,OTHERSYS,streamZ,WPB,1,HK,HASE,appZ,zoe,2024-01-01,zoe,2024-01-01,Y,N\n",
]
ROUTING = "use_case_id,topic\nUC001,alerts.sms.topic\n"
MESSAGES = (
    "producer_repo,destination,consumer_repo,routing_source,evidence\n"
    "svc-a,alerts.sms.topic,svc-b,annotation,src/A.java:1\n"
)
REPO_TAGS = {
    "svc-a": {"system": "hase", "channel": ["sms"], "mode": "api", "tokens": [], "bundle": "b"},
    "svc-b": {"system": "hase", "channel": ["sms"], "mode": "job", "tokens": [], "bundle": "b"},
}


class _FixtureMixin:
    def _write_fixture_root(self, root):
        recon_dir = os.path.join(root, "recon_out")
        index_dir = os.path.join(root, "index")
        os.makedirs(recon_dir, exist_ok=True)
        os.makedirs(index_dir, exist_ok=True)
        with open(os.path.join(recon_dir, "internal_edges.csv"), "w", encoding="utf-8", newline="") as h:
            h.write("from_repo,to_repo\n")
        with open(os.path.join(index_dir, "message_edges.csv"), "w", encoding="utf-8", newline="") as h:
            h.write(MESSAGES)
        with open(os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv"), "w",
                  encoding="utf-8", newline="") as h:
            h.write(ROUTING)
        with open(os.path.join(index_dir, "tbl_use_case.snapshot.csv"), "w", encoding="utf-8", newline="") as h:
            h.write(MASTER_HEADER)
            h.writelines(MASTER_ROWS)
        with open(os.path.join(index_dir, "repo_tags.json"), "w", encoding="utf-8") as h:
            json.dump(REPO_TAGS, h)
        with open(os.path.join(index_dir, "glossary.json"), "w", encoding="utf-8") as h:
            json.dump({}, h)
        return recon_dir, index_dir

    def _patch_config(self, stack, root, recon_dir, index_dir, with_master=True):
        stack.enter_context(mock.patch.object(rconfig, "ROOT", root))
        stack.enter_context(mock.patch.object(rconfig, "INDEX_DIR", index_dir))
        stack.enter_context(mock.patch.object(rconfig, "RECON_DIR", recon_dir))
        stack.enter_context(mock.patch.object(rconfig, "EDGES_CSV", os.path.join(recon_dir, "internal_edges.csv")))
        stack.enter_context(mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(index_dir, "message_edges.csv")))
        stack.enter_context(mock.patch.object(
            rconfig, "USECASE_SNAPSHOT_CSV",
            os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv"),
        ))
        master_path = (os.path.join(index_dir, "tbl_use_case.snapshot.csv") if with_master
                       else os.path.join(index_dir, "absent.csv"))
        stack.enter_context(mock.patch.object(rconfig, "USECASE_MASTER_CSV", master_path))
        stack.enter_context(mock.patch.object(rconfig, "SOURCE_SYSTEM_ALIASES_JSON", os.path.join(index_dir, "absent-aliases.json")))
        stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(index_dir, "repo_tags.json")))
        stack.enter_context(mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(index_dir, "glossary.json")))


class SourceSystemReportTests(_FixtureMixin, unittest.TestCase):
    def test_parse_target_recognizes_source_system(self):
        self.assertEqual(impact_report.parse_target("source-system:PEGA"), ("source-system", "PEGA"))

    def test_source_system_report_splits_routed_and_catalog_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir)
                report = impact_report.build_report("source-system:PEGA")

        self.assertEqual(report["target"]["use_case_count"], 2)
        self.assertEqual(report["target"]["routed_count"], 1)
        self.assertEqual(report["target"]["catalog_only_count"], 1)
        routed_ids = {item["use_case_id"] for item in report["use_cases"]["routed"]}
        catalog_ids = {item["use_case_id"] for item in report["use_cases"]["catalog_only"]}
        self.assertEqual(routed_ids, {"UC001"})
        self.assertEqual(catalog_ids, {"UC002"})
        self.assertIn("1/2", report["confidence_banner"])
        self.assertEqual(report["owners"], ["alice", "bob"])
        self.assertIn("sms", [item["channel"] for item in report["channel_chain"]])
        self.assertTrue(report["citations"])
        # every use-case item is individually cited
        for item in report["use_cases"]["routed"] + report["use_cases"]["catalog_only"]:
            self.assertTrue(item["citation"])

    def test_unknown_source_system_is_a_clean_error_not_a_stack_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = impact_report.main(["source-system:NOPE"])
                self.assertEqual(exit_code, 1)
                self.assertIn("unknown target", stdout.getvalue())
                self.assertNotIn("Traceback", stdout.getvalue())

    def test_source_system_report_renders_markdown_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir)
                report = impact_report.build_report("source-system:PEGA")
                text = impact_report.render_report_markdown(report)
        self.assertIn("Routed use cases", text)
        self.assertIn("Catalog-only use cases", text)
        self.assertIn("UC001", text)
        self.assertIn("UC002", text)


class UsecaseEnrichmentRegressionTests(_FixtureMixin, unittest.TestCase):
    def test_enrichment_present_populates_business_and_governance(self):
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir, with_master=True)
                report = impact_report.build_report("use-case:UC001")

        self.assertEqual(report["target"]["description"], "UC001 — Alpha Case")
        self.assertEqual(report["target"]["business"]["source_system"], "PEGA")
        self.assertTrue(report["target"]["business"]["citation"])
        self.assertIn(report["target"]["business"]["citation"], report["target"]["citations"])
        self.assertIn("status", report["target"]["governance"])
        self.assertIn("checks", report["consent_preflight"])

    def test_enrichment_absent_is_byte_identical_to_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir, with_master=True)
                enriched = impact_report.build_report("use-case:UC001")
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir, with_master=False)
                baseline = impact_report.build_report("use-case:UC001")

        self.assertEqual(baseline["target"]["description"], "UC001")
        self.assertNotIn("business", baseline["target"])
        self.assertNotIn("governance", baseline["target"])
        self.assertNotIn("consent_preflight", baseline)
        # everything else (routes/upstream/downstream/channel_chain) must be untouched
        self.assertEqual(baseline["upstream"], enriched["upstream"])
        self.assertEqual(baseline["downstream"], enriched["downstream"])
        self.assertEqual(baseline["async_routes"], enriched["async_routes"])
        self.assertEqual(baseline["channel_chain"], enriched["channel_chain"])

    def test_outage_report_affected_use_cases_enriched_when_master_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir, with_master=True)
                items = outage_report.affected_use_cases([{"topic": "alerts.sms.topic"}])
        item = next(i for i in items if i["use_case"] == "UC001")
        self.assertEqual(item["name"], "Alpha Case")
        self.assertEqual(item["source_system"], "PEGA")
        self.assertEqual(item["owner"], "alice")

    def test_outage_report_affected_use_cases_unchanged_when_master_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir, with_master=False)
                items = outage_report.affected_use_cases([{"topic": "alerts.sms.topic"}])
        item = next(i for i in items if i["use_case"] == "UC001")
        self.assertNotIn("name", item)
        self.assertNotIn("source_system", item)
        self.assertNotIn("owner", item)


if __name__ == "__main__":
    unittest.main()
