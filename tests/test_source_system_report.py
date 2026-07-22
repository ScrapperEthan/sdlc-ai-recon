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


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join(row) + "\n")


def _write_manifest_dataset(base_dir, environment="UAT", snapshot_id="20260722",
                             master=None, rule=None, ext=None, route=None):
    """Round A manifest-driven dataset (config.USECASE_DATASET_DIR), distinct from the legacy
    single-file USECASE_MASTER_CSV fixture used elsewhere in this file — needed here because
    endpoint-repo resolution (follow-up #3) requires a tbl_use_case_ext table."""
    dataset_dir = os.path.join(base_dir, "index", "usecase-snapshots", "active")
    os.makedirs(dataset_dir, exist_ok=True)
    tables = {}
    for name, payload, filename in (
        ("tbl_use_case", master, "tbl_use_case.snapshot.csv"),
        ("tbl_use_case_channel_rule", rule, "tbl_use_case_channel_rule.snapshot.csv"),
        ("tbl_use_case_ext", ext, "tbl_use_case_ext.snapshot.csv"),
        ("tbl_event_router_usecase_topic", route, "tbl_event_router_usecase_topic.snapshot.csv"),
    ):
        if not payload:
            continue
        header, rows = payload
        path = os.path.join(dataset_dir, filename)
        _write_csv(path, header, rows)
        tables[name] = {"file": filename, "row_count": len(rows)}
    manifest = {"environment": environment, "snapshot_id": snapshot_id,
                "exported_at": "2026-07-22T00:00:00+08:00", "tables": tables}
    with open(os.path.join(dataset_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    return dataset_dir

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
        # No manifest dir in this fixture -> active_dataset() must fall through to the legacy
        # USECASE_MASTER_CSV path patched below, not an ambient SDLC_USECASE_DATASET pointing at
        # a real box dataset (RUNBOOK-45 Part A follow-up #1: test isolation).
        stack.enter_context(mock.patch.object(
            rconfig, "USECASE_DATASET_DIR", os.path.join(root, "no-manifest-here")))
        stack.enter_context(mock.patch.object(rconfig, "USECASE_MASTER_CSV", master_path))
        stack.enter_context(mock.patch.object(rconfig, "SOURCE_SYSTEM_ALIASES_JSON", os.path.join(index_dir, "absent-aliases.json")))
        stack.enter_context(mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(index_dir, "repo_tags.json")))
        stack.enter_context(mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(index_dir, "glossary.json")))


class SourceSystemReportTests(_FixtureMixin, unittest.TestCase):
    def test_parse_target_recognizes_source_system(self):
        self.assertEqual(impact_report.parse_target("source-system:PEGA"), ("source-system", "PEGA"))

    def test_source_system_report_coverage_funnel_and_layered_owners(self):
        # This fixture has no tbl_use_case_channel_rule/tbl_use_case_ext (legacy back-compat
        # dataset — pre-Round-A exports only ever had tbl_use_case), so both members are
        # catalog_only under the new coverage funnel (B6), which REPLACES the old routed-vs-
        # catalog-only split that read has_route off the dev/SCT snapshot regardless of which
        # environment the active dataset actually declared (defect #2). has_route/async routes
        # still populate here because this fixture's route snapshot IS same-environment (legacy
        # mode has exactly one file in play, never a cross-environment join).
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir, index_dir = self._write_fixture_root(tmp)
            with ExitStack() as stack:
                self._patch_config(stack, tmp, recon_dir, index_dir)
                report = impact_report.build_report("source-system:PEGA")

        target = report["target"]
        self.assertEqual(target["use_case_count"], 2)
        self.assertEqual(target["active_count"], 2)
        self.assertEqual(target["inactive_count"], 0)
        self.assertEqual(target["coverage"]["configured"], 0)
        self.assertEqual(target["coverage"]["catalog_only"], 2)
        self.assertTrue(target["route_dimension"]["available"])
        ids = {item["use_case_id"] for item in report["use_cases"]["items"]}
        self.assertEqual(ids, {"UC001", "UC002"})
        has_route = {item["use_case_id"]: item["has_route"] for item in report["use_cases"]["items"]}
        self.assertTrue(has_route["UC001"])
        self.assertFalse(has_route["UC002"])
        self.assertIn("catalog_only", report["confidence_banner"])
        # No Ext table -> only config_maintainers (created_by/modified_by) populate (defect #7).
        self.assertEqual(report["owners"], {
            "business_owners": [], "cost_governance": [], "config_maintainers": ["alice", "bob"],
        })
        self.assertIn("sms", [item["channel"] for item in report["channel_chain"]])
        self.assertTrue(report["citations"])
        # every use-case item is individually cited
        for item in report["use_cases"]["items"]:
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
        self.assertIn("Coverage funnel", text)
        self.assertIn("Use cases (", text)
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


class EndpointRepoMarkdownTests(unittest.TestCase):
    """RUNBOOK-45 Part A follow-up #3: the CLI markdown dropped the resolved endpoint repo name(s)
    and only printed a bare entrypoint_traceable=True/False — the repo name is the upstream payload."""

    def _dataset(self, tmp):
        master = (["use_case_id", "use_case_name", "source_system", "status"],
                   [["UC001", "Alpha Case", "PEGA", "Y"]])
        ext = (["use_case_id", "endpoint"],
               [["UC001", "mc-hk-hase-pega-adapter-job"]])
        return _write_manifest_dataset(tmp, master=master, ext=ext)

    def test_usecase_markdown_shows_resolved_repo_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp)
            repos_txt = os.path.join(tmp, "repos.txt")
            with open(repos_txt, "w", encoding="utf-8") as handle:
                handle.write("mc-hk-hase-pega-adapter-job\n")
            with mock.patch.object(rconfig, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(rconfig, "ROOT", tmp), \
                 mock.patch.object(rconfig, "REPOS_TXT", repos_txt), \
                 mock.patch.object(rconfig, "USECASE_SNAPSHOT_CSV", os.path.join(tmp, "absent-route.csv")):
                report = impact_report.build_report("use-case:UC001")
                markdown = impact_report.render_report_markdown(report)

        self.assertEqual(report["endpoint_repos"][0]["repo"], "mc-hk-hase-pega-adapter-job")
        self.assertIn("mc-hk-hase-pega-adapter-job", markdown)
        self.assertIn("Endpoint Repos", markdown)

    def test_source_system_markdown_shows_resolved_repo_name_per_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp)
            repos_txt = os.path.join(tmp, "repos.txt")
            with open(repos_txt, "w", encoding="utf-8") as handle:
                handle.write("mc-hk-hase-pega-adapter-job\n")
            with mock.patch.object(rconfig, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(rconfig, "ROOT", tmp), \
                 mock.patch.object(rconfig, "REPOS_TXT", repos_txt), \
                 mock.patch.object(rconfig, "USECASE_SNAPSHOT_CSV", os.path.join(tmp, "absent-route.csv")):
                report = impact_report.build_report("source-system:PEGA")
                markdown = impact_report.render_report_markdown(report)

        self.assertIn("mc-hk-hase-pega-adapter-job", markdown)
        self.assertNotIn("entrypoint_traceable=True", markdown)  # bare bool no longer printed


class RuleTextAstAndValidationWiringTests(unittest.TestCase):
    """Round B1/B3 wired into the existing use-case report: rule_text AST + consistency findings,
    additive and null-safe (byte-identical to today when Ext/rules are absent)."""

    def test_rule_text_ast_and_validation_populate_when_ext_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "use_case_name", "status"], [["I0141", "Case", "Y"]])
            rule = (["use_case_id", "channel", "priority", "route", "router",
                     "traffic_percentage", "tag", "sender", "send_policy", "status"], [
                ["I0141", "LETTER", "1", "R", "RT", "100", "T", "SYS", "IMMEDIATE", "Y"],
                ["I0141", "EMAIL", "2", "R", "RT", "100", "T", "SYS", "IMMEDIATE", "Y"],
                ["I0141", "SMS", "3", "R", "RT", "100", "T", "SYS", "IMMEDIATE", "Y"],
            ])
            ext = (["use_case_id", "rule_text"], [["I0141", "LETTER > (EMAIL & SMS)"]])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(rconfig, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(rconfig, "ROOT", tmp), \
                 mock.patch.object(rconfig, "USECASE_SNAPSHOT_CSV", os.path.join(tmp, "absent.csv")):
                report = impact_report.build_report("use-case:I0141")
                markdown = impact_report.render_report_markdown(report)

        self.assertEqual(report["rule_text_ast"]["mode"], "MIXED")
        self.assertEqual(report["rule_text_ast"]["semantics"], "unconfirmed")
        self.assertNotIn("rule_text_interpretation", report)  # unconfirmed by default -> no assertion
        findings = {f["check"] for f in report["validation_findings"]}
        self.assertIn("expression_vs_priority", findings)  # the I0141 canonical mismatch
        self.assertIn("Channel Decision Expression", markdown)
        self.assertIn("semantics: **unconfirmed**", markdown)
        self.assertIn("Validation Findings", markdown)
        self.assertIn("expression_vs_priority", markdown)

    def test_absent_master_still_byte_identical_no_new_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(rconfig, "USECASE_DATASET_DIR", os.path.join(tmp, "no-manifest")), \
                 mock.patch.object(rconfig, "USECASE_MASTER_CSV", os.path.join(tmp, "absent.csv")), \
                 mock.patch.object(rconfig, "USECASE_SNAPSHOT_CSV", os.path.join(tmp, "absent2.csv")), \
                 mock.patch.object(rconfig, "ROOT", tmp):
                report = impact_report.build_report("use-case:UC999")
        self.assertNotIn("rule_text_ast", report)
        self.assertNotIn("rule_text_interpretation", report)
        self.assertNotIn("validation_findings", report)


class SourceSystemCliPagingTests(unittest.TestCase):
    """RUNBOOK-45 Part A follow-up #4: a direct CLI call to a source-system target used to dump
    every member (e.g. ~880 for MDC) because parse_args never exposed offset/limit."""

    def _dataset(self, tmp, count=60):
        header = ["use_case_id", "use_case_name", "source_system", "status"]
        rows = [[f"UC{i:03d}", f"Case {i}", "PEGA", "Y"] for i in range(count)]
        return _write_manifest_dataset(tmp, master=(header, rows))

    def _run(self, tmp, dataset_dir, argv):
        stdout = io.StringIO()
        with mock.patch.object(rconfig, "USECASE_DATASET_DIR", dataset_dir), \
             mock.patch.object(rconfig, "ROOT", tmp), \
             mock.patch.object(rconfig, "USECASE_SNAPSHOT_CSV", os.path.join(tmp, "absent-route.csv")), \
             contextlib.redirect_stdout(stdout):
            exit_code = impact_report.main(argv)
        return exit_code, stdout.getvalue()

    def test_default_caps_at_50_not_a_full_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp, count=60)
            exit_code, out = self._run(tmp, dataset_dir, ["source-system:PEGA", "--out", ""])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Use cases (60/60)", out)  # not a full unlabelled dump
        self.assertIn("Use cases (50/60, truncated)", out)

    def test_explicit_limit_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp, count=60)
            exit_code, out = self._run(
                tmp, dataset_dir, ["source-system:PEGA", "--limit", "5", "--out", ""])
        self.assertEqual(exit_code, 0)
        self.assertIn("Use cases (5/60, truncated)", out)

    def test_limit_zero_means_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp, count=60)
            exit_code, out = self._run(
                tmp, dataset_dir, ["source-system:PEGA", "--limit", "0", "--out", ""])
        self.assertEqual(exit_code, 0)
        self.assertIn("Use cases (60/60)", out)
        self.assertNotIn("truncated", out)

    def test_include_inactive_and_offset_flow_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp, count=10)
            exit_code, out = self._run(
                tmp, dataset_dir,
                ["source-system:PEGA", "--offset", "5", "--limit", "3", "--include-inactive", "--out", ""])
        self.assertEqual(exit_code, 0)
        self.assertIn("Use cases (3/10, truncated)", out)

    def test_non_source_system_target_unaffected_by_paging_defaults(self):
        # Non-source-system CLI calls must not be capped/broken by the new offset/limit plumbing.
        with tempfile.TemporaryDirectory() as tmp:
            recon_dir = os.path.join(tmp, "recon_out")
            index_dir = os.path.join(tmp, "index")
            os.makedirs(recon_dir, exist_ok=True)
            os.makedirs(index_dir, exist_ok=True)
            with open(os.path.join(recon_dir, "internal_edges.csv"), "w", encoding="utf-8", newline="") as h:
                h.write("from_repo,to_repo\nsvc-a,svc-b\n")
            with mock.patch.object(rconfig, "ROOT", tmp), \
                 mock.patch.object(rconfig, "RECON_DIR", recon_dir), \
                 mock.patch.object(rconfig, "INDEX_DIR", index_dir), \
                 mock.patch.object(rconfig, "EDGES_CSV", os.path.join(recon_dir, "internal_edges.csv")), \
                 mock.patch.object(rconfig, "MESSAGE_EDGES_CSV", os.path.join(index_dir, "absent.csv")), \
                 mock.patch.object(rconfig, "USECASE_DATASET_DIR", os.path.join(index_dir, "no-manifest")), \
                 mock.patch.object(rconfig, "USECASE_MASTER_CSV", os.path.join(index_dir, "absent.csv")), \
                 mock.patch.object(rconfig, "USECASE_SNAPSHOT_CSV", os.path.join(index_dir, "absent.csv")), \
                 mock.patch.object(rconfig, "REPO_TAGS_JSON", os.path.join(index_dir, "absent.json")), \
                 mock.patch.object(rconfig, "GLOSSARY_JSON", os.path.join(index_dir, "absent.json")):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = impact_report.main(["svc-a", "--out", ""])
        self.assertEqual(exit_code, 0)
        self.assertIn("Impact Report", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
