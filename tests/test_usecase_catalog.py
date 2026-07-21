"""Round A (UAT Use Case catalog) — new-module tests for retriever/usecase_catalog.py.

Covers the P0 defect fixes directly: environment provenance (#1), the cross-environment route
guard (#2), exact-column-wins binding (#3/#4), source_system canonicalization (#5-ish), active
filter + layered owners (#5/#7), the coverage funnel (#6), and the endpoint resolver (#8's arch
data). See docs/specs/use-case-uat-catalog.md.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import config, usecase_catalog as uc


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join(row) + "\n")


def _write_manifest_dataset(base_dir, environment="UAT", snapshot_id="20260720-1730",
                             master=None, rule=None, ext=None, route=None):
    """Writes a manifest-driven dataset dir under base_dir and returns its path. `master`/`rule`/
    `ext`/`route` are each (header_list, rows_list_of_lists) or None to omit that table."""
    dataset_dir = os.path.join(base_dir, "index", "usecase-snapshots", "active")
    os.makedirs(dataset_dir, exist_ok=True)
    tables = {}
    specs = (
        ("tbl_use_case", master, "tbl_use_case.snapshot.csv"),
        ("tbl_use_case_channel_rule", rule, "tbl_use_case_channel_rule.snapshot.csv"),
        ("tbl_use_case_ext", ext, "tbl_use_case_ext.snapshot.csv"),
        ("tbl_event_router_usecase_topic", route, "tbl_event_router_usecase_topic.snapshot.csv"),
    )
    for name, payload, filename in specs:
        if not payload:
            continue
        header, rows = payload
        path = os.path.join(dataset_dir, filename)
        _write_csv(path, header, rows)
        tables[name] = {"file": filename, "row_count": len(rows)}
    manifest = {
        "environment": environment, "snapshot_id": snapshot_id,
        "exported_at": "2026-07-20T17:30:00+08:00", "tables": tables,
    }
    with open(os.path.join(dataset_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    return dataset_dir


class ResolveColumnTests(unittest.TestCase):
    """Building block 2: exact -> alias -> unique-fuzzy -> ambiguity."""

    def test_exact_status_wins_over_bounce_back_substring(self):
        # defect #3: the old "first column containing the needle" logic picked whichever of these
        # came first in the CSV; exact match must win regardless of column order.
        cols = ["use_case_id", "unknown_bounce_back_status", "status"]
        col, ambiguous = uc.resolve_column(cols, "status")
        self.assertEqual(col, "status")
        self.assertEqual(ambiguous, [])
        # order reversed -> same result
        cols = ["status", "unknown_bounce_back_status"]
        col, ambiguous = uc.resolve_column(cols, "status")
        self.assertEqual(col, "status")

    def test_fuzzy_fallback_only_when_unique_and_no_exact_present(self):
        cols = ["use_case_id", "unknown_bounce_back_status"]
        col, ambiguous = uc.resolve_column(cols, "status")
        self.assertEqual(col, "unknown_bounce_back_status")

    def test_ambiguous_binds_none_and_lists_every_candidate(self):
        cols = ["source_system", "Source_System"]  # a genuine header drift/duplicate
        col, ambiguous = uc.resolve_column(cols, "source_system")
        self.assertIsNone(col)
        self.assertEqual(set(ambiguous), {"source_system", "Source_System"})

    def test_consent_accepts_both_singular_and_plural_insight(self):
        # defect #4: UAT header uses the SINGULAR "insight".
        for header in ("marketing_insight_push_optin_flag", "marketing_insights_push_optin_flag"):
            col, ambiguous = uc.resolve_column([header], "marketing_insight_push_optin_flag")
            self.assertEqual(col, header)
            self.assertEqual(ambiguous, [])

    def test_unknown_field_binds_nothing_not_a_crash(self):
        col, ambiguous = uc.resolve_column(["a", "b"], "not_a_real_field")
        self.assertIsNone(col)
        self.assertEqual(ambiguous, [])


class DatasetManifestTests(unittest.TestCase):
    """Building block 1: manifest-driven dataset + legacy back-compat."""

    def test_manifest_environment_is_real_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status"], [["UC001", "PEGA", "Y"]])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                manifest = uc.snapshot_manifest()
        self.assertEqual(manifest["environment"], "UAT")
        self.assertEqual(manifest["snapshot_id"], "20260720-1730")
        self.assertFalse(manifest["production_verified"])
        self.assertIn("column_bindings", manifest)

    def test_legacy_backcompat_environment_is_unknown_not_dev_sct(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy_dir = os.path.join(tmp, "index")
            os.makedirs(legacy_dir, exist_ok=True)
            legacy_csv = os.path.join(legacy_dir, "tbl_use_case.snapshot.csv")
            with open(legacy_csv, "w", encoding="utf-8", newline="") as handle:
                handle.write("use_case_id,source_system,status\nUC001,PEGA,Y\n")
            missing_dataset_dir = os.path.join(tmp, "index", "usecase-snapshots", "active")
            saved = os.environ.pop("SDLC_USECASE_ENV", None)
            try:
                with mock.patch.object(config, "USECASE_DATASET_DIR", missing_dataset_dir), \
                     mock.patch.object(config, "USECASE_MASTER_CSV", legacy_csv), \
                     mock.patch.object(config, "ROOT", tmp):
                    manifest = uc.snapshot_manifest()
            finally:
                if saved is not None:
                    os.environ["SDLC_USECASE_ENV"] = saved
        self.assertEqual(manifest["environment"], "unknown")
        self.assertNotEqual(manifest["environment"], "dev/SCT")

    def test_neither_manifest_nor_legacy_is_a_clean_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_dataset_dir = os.path.join(tmp, "index", "usecase-snapshots", "active")
            missing_legacy = os.path.join(tmp, "index", "tbl_use_case.snapshot.csv")
            with mock.patch.object(config, "USECASE_DATASET_DIR", missing_dataset_dir), \
                 mock.patch.object(config, "USECASE_MASTER_CSV", missing_legacy):
                self.assertIsNone(uc.master_for("UC001"))
                report = uc.quality_report()
        self.assertFalse(report["available"])


class CrossEnvironmentGuardTests(unittest.TestCase):
    """Building block 1: the P0 that mattered most (defect #2) — RUNBOOK-45 Step A4."""

    def test_uat_dataset_without_own_route_table_never_joins_dev_sct(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A stray "old dev/SCT" route snapshot sitting at the pre-Round-A legacy path, still
            # discoverable on disk -- it must NEVER get silently joined against a UAT dataset.
            legacy_route = os.path.join(tmp, "index", "tbl_event_router_usecase_topic.snapshot.csv")
            os.makedirs(os.path.dirname(legacy_route), exist_ok=True)
            with open(legacy_route, "w", encoding="utf-8", newline="") as handle:
                handle.write("use_case_id,topic\nUC001,alerts.sms.topic\n")

            master = (["use_case_id", "source_system", "status"],
                      [["UC001", "PEGA", "Y"], ["UC002", "PEGA", "Y"]])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master)  # no route table

            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "USECASE_SNAPSHOT_CSV", legacy_route), \
                 mock.patch.object(config, "ROOT", tmp):
                route = uc.route_dimension()
                out = uc.use_cases_for_source_system("PEGA")

        self.assertFalse(route["available"])
        self.assertEqual(route["reason"], "no same-environment route snapshot")
        for item in out["items"]:
            self.assertNotIn("has_route", item)  # never a silent count off the wrong environment

    def test_uat_dataset_with_own_route_table_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status"],
                      [["UC001", "PEGA", "Y"], ["UC002", "PEGA", "Y"]])
            route = (["use_case_id", "topic"], [["UC001", "alerts.sms.topic"]])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master, route=route)

            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                dim = uc.route_dimension()
                out = uc.use_cases_for_source_system("PEGA")

        self.assertTrue(dim["available"])
        by_id = {item["use_case_id"]: item for item in out["items"]}
        self.assertTrue(by_id["UC001"]["has_route"])
        self.assertFalse(by_id["UC002"]["has_route"])


class CanonicalizationTests(unittest.TestCase):
    """Building block 4."""

    def test_format_variants_fold_without_an_alias_file(self):
        variants = ["eAlert", "ealert", "EAlert", "E-alert", "e-Alert"]
        canonicals = {uc.canonicalize_source_system(v)["canonical"] for v in variants}
        self.assertEqual(len(canonicals), 1)

    def test_powercard_variants_fold(self):
        self.assertEqual(
            uc.canonicalize_source_system("PowerCard")["canonical"],
            uc.canonicalize_source_system("Power Card")["canonical"],
        )

    def test_mdc_test_not_merged_into_mdc(self):
        self.assertNotEqual(
            uc.canonicalize_source_system("MDC")["canonical"],
            uc.canonicalize_source_system("MDC Test")["canonical"],
        )

    def test_alias_override_folds_a_genuinely_different_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            aliases_path = os.path.join(tmp, "source_system_aliases.json")
            with open(aliases_path, "w", encoding="utf-8") as handle:
                json.dump({"PEGA": ["Pega", "PEGA_HK"]}, handle)
            with mock.patch.object(config, "SOURCE_SYSTEM_ALIASES_JSON", aliases_path):
                pega = uc.canonicalize_source_system("PEGA")
                pega_hk = uc.canonicalize_source_system("PEGA_HK")
        self.assertEqual(pega["canonical"], pega_hk["canonical"])
        self.assertEqual(pega["display_name"], "PEGA")

    def test_source_systems_groups_by_canonical_with_raw_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status"], [
                ["UC001", "eAlert", "Y"], ["UC002", "e-Alert", "Y"],
                ["UC003", "MDC", "Y"], ["UC004", "MDC_Test", "Y"],
            ])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                items = uc.source_systems()

        by_canon = {item["canonical"]: item for item in items}
        ealert_canon = uc.canonicalize_source_system("eAlert")["canonical"]
        mdc_canon = uc.canonicalize_source_system("MDC")["canonical"]
        mdctest_canon = uc.canonicalize_source_system("MDC_Test")["canonical"]
        self.assertEqual(len(items), 3)
        self.assertEqual(sorted(by_canon[ealert_canon]["raw_variants"]), ["e-Alert", "eAlert"])
        self.assertEqual(by_canon[mdc_canon]["use_case_count"], 1)
        self.assertEqual(by_canon[mdctest_canon]["use_case_count"], 1)
        self.assertNotEqual(mdc_canon, mdctest_canon)


class RuleExtIngestTests(unittest.TestCase):
    """Building block 3."""

    def test_rules_by_use_case_id_keeps_facts_and_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status"], [["UC001", "PEGA", "Y"]])
            rule = (["use_case_id", "channel", "priority", "route", "router",
                     "traffic_percentage", "tag", "sender", "send_policy", "status"], [
                ["UC001", "SMS", "1", "R1", "RT1", "100", "T1", "SYS", "IMMEDIATE", "Y"],
                ["UC001", "EMAIL", "2", "R2", "RT2", "100", "T2", "SYS", "IMMEDIATE", "Y"],
            ])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master, rule=rule)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                rules = uc.rules_by_use_case_id()
                channels = uc.channels_for_use_case("uc001")

        self.assertEqual(len(rules["uc001"]), 2)
        self.assertEqual(channels, ["EMAIL", "SMS"])
        self.assertTrue(rules["uc001"][0]["citation"].endswith(":2"))
        self.assertTrue(rules["uc001"][1]["citation"].endswith(":3"))

    def test_ext_missing_dormant_period_is_schema_drift_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status"], [["UC001", "PEGA", "Y"]])
            ext = (["use_case_id", "rule_text", "endpoint", "message_owner"],
                   [["UC001", "LETTER-priority-chain", "svc-a->svc-b", "Alice Wong"]])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                ext_idx = uc.ext_by_use_case_id()

        self.assertEqual(ext_idx["uc001"]["dormant_period"], "")
        self.assertEqual(ext_idx["uc001"]["rule_text"], "LETTER-priority-chain")
        self.assertEqual(ext_idx["uc001"]["message_owner"], "Alice Wong")

    def test_missing_channel_rule_table_is_empty_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status"], [["UC001", "PEGA", "Y"]])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                self.assertEqual(uc.rules_by_use_case_id(), {})
                self.assertEqual(uc.ext_by_use_case_id(), {})
                self.assertEqual(uc.channels_for_use_case("UC001"), [])


class EndpointResolverTests(unittest.TestCase):
    """Building block 7."""

    def test_exact_normalized_and_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos_txt = os.path.join(tmp, "repos.txt")
            with open(repos_txt, "w", encoding="utf-8") as handle:
                handle.write("mc-hk-hase-ingress-api\nmc-hk-hase-pega-adapter-job\n")
            with mock.patch.object(config, "REPOS_TXT", repos_txt):
                segs = uc.resolve_endpoint(
                    "mc-hk-hase-ingress-api->MC_HK_HASE_PEGA_ADAPTER_JOB->unknown-svc-xyz")
        self.assertEqual(segs[0]["confidence"], "declared-exact")
        self.assertEqual(segs[0]["repo"], "mc-hk-hase-ingress-api")
        self.assertEqual(segs[1]["confidence"], "declared-normalized")
        self.assertEqual(segs[1]["repo"], "mc-hk-hase-pega-adapter-job")
        self.assertEqual(segs[2]["confidence"], "unresolved")
        self.assertIsNone(segs[2]["repo"])
        self.assertEqual(segs[2]["raw"], "unknown-svc-xyz")  # raw evidence kept

    def test_version_tokens_are_never_treated_as_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos_txt = os.path.join(tmp, "repos.txt")
            with open(repos_txt, "w", encoding="utf-8") as handle:
                handle.write("mc-hk-hase-ingress-api\n")
            with mock.patch.object(config, "REPOS_TXT", repos_txt):
                segs = uc.resolve_endpoint("mc-hk-hase-ingress-api->v3->_v4->downstream-svc")
        confidences = [s["confidence"] for s in segs]
        self.assertEqual(
            confidences,
            ["declared-exact", "version_annotation", "version_annotation", "unresolved"],
        )
        self.assertIsNone(segs[1]["repo"])
        self.assertIsNone(segs[2]["repo"])
        self.assertNotEqual(segs[1]["repo"], "v3")  # never a repo literally named "v3"

    def test_blank_endpoint_returns_empty(self):
        self.assertEqual(uc.resolve_endpoint(""), [])
        self.assertEqual(uc.resolve_endpoint(None), [])


class OwnersLayeredTests(unittest.TestCase):
    """Building block 5 (defect #7)."""

    def test_layered_owners_business_over_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status", "created_by", "modified_by"],
                      [["UC001", "PEGA", "Y", "alice", "alice"]])
            ext = (["use_case_id", "message_owner", "business_contact", "cost_owner", "signoff_by"],
                   [["UC001", "Bob Chan", "Carol Li", "Finance Team", "Dan Ho"]])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                owners = uc.owners_for(["UC001"])

        self.assertEqual(owners["business_owners"], ["Bob Chan", "Carol Li"])
        self.assertEqual(owners["cost_governance"], ["Dan Ho", "Finance Team"])
        self.assertEqual(owners["config_maintainers"], ["alice"])

    def test_missing_ext_only_config_maintainers_populate(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "source_system", "status", "created_by", "modified_by"],
                      [["UC001", "PEGA", "Y", "alice", "bob"]])
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                owners = uc.owners_for(["UC001"])

        self.assertEqual(owners, {"business_owners": [], "cost_governance": [],
                                   "config_maintainers": ["alice", "bob"]})

    def test_empty_input_is_clean_not_a_crash(self):
        self.assertEqual(
            uc.owners_for([]),
            {"business_owners": [], "cost_governance": [], "config_maintainers": []},
        )


class CoverageFunnelAndPaginationTests(unittest.TestCase):
    """Building blocks 5, 6, 8."""

    def _dataset(self, tmp):
        master = (["use_case_id", "source_system", "status"], [
            ["UC001", "SYS", "Y"],  # configured (has a channel rule)
            ["UC002", "SYS", "Y"],  # expression_ready (ext.rule_text, no rule)
            ["UC003", "SYS", "Y"],  # entrypoint_traceable (ext.endpoint resolves, no rule_text)
            ["UC004", "SYS", "Y"],  # catalog_only (no rule, no ext)
            ["UC005", "SYS", "N"],  # inactive, also catalog_only
        ])
        rule = (["use_case_id", "channel", "priority", "route", "router",
                 "traffic_percentage", "tag", "sender", "send_policy", "status"],
                [["UC001", "SMS", "1", "R1", "RT1", "100", "T1", "SYS", "IMMEDIATE", "Y"]])
        ext = (["use_case_id", "rule_text", "endpoint", "message_owner"], [
            ["UC002", "LETTER-ONLY", "", ""],
            ["UC003", "", "svc-a", "Message Owner Three"],
        ])
        return _write_manifest_dataset(tmp, environment="UAT", master=master, rule=rule, ext=ext)

    def test_coverage_funnel_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp)
            repos_txt = os.path.join(tmp, "repos.txt")
            with open(repos_txt, "w", encoding="utf-8") as handle:
                handle.write("svc-a\n")
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp), \
                 mock.patch.object(config, "REPOS_TXT", repos_txt):
                coverage = uc.source_system_coverage("SYS")

        self.assertEqual(coverage["total"], 5)
        self.assertEqual(coverage["active"], 4)
        self.assertEqual(coverage["configured"], 1)
        self.assertEqual(coverage["expression_ready"], 1)
        self.assertEqual(coverage["entrypoint_traceable"], 1)
        self.assertEqual(coverage["catalog_only"], 2)

    def test_active_filter_default_and_include_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                default_out = uc.use_cases_for_source_system("SYS")
                all_out = uc.use_cases_for_source_system("SYS", include_inactive=True)

        self.assertEqual(default_out["total"], 5)         # total always the FULL population
        self.assertEqual(default_out["active_count"], 4)
        self.assertEqual(default_out["inactive_count"], 1)
        self.assertEqual({i["use_case_id"] for i in default_out["items"]},
                          {"UC001", "UC002", "UC003", "UC004"})  # UC005 excluded by default
        self.assertEqual({i["use_case_id"] for i in all_out["items"]},
                          {"UC001", "UC002", "UC003", "UC004", "UC005"})
        uc005 = next(i for i in all_out["items"] if i["use_case_id"] == "UC005")
        self.assertFalse(uc005["active"])

    def test_offset_limit_paginate_without_dropping_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_dir = self._dataset(tmp)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                page1 = uc.use_cases_for_source_system("SYS", limit=2)
                page2 = uc.use_cases_for_source_system("SYS", offset=2, limit=2)

        self.assertEqual(page1["total"], 5)
        self.assertEqual(page1["returned"], 2)
        self.assertTrue(page1["truncated"])
        self.assertEqual(page2["returned"], 2)
        self.assertFalse(page2["truncated"])  # last page of the 4 active items (offset 2 + 2 = 4)
        ids_seen = {i["use_case_id"] for i in page1["items"]} | {i["use_case_id"] for i in page2["items"]}
        self.assertEqual(ids_seen, {"UC001", "UC002", "UC003", "UC004"})


class QualityReportFullDatasetTests(unittest.TestCase):
    """Building block 10."""

    def test_illegal_codes_column_bindings_and_route_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (
                ["use_case_id", "use_case_name", "source_system", "business_category",
                 "work_stream_name", "status", "unknown_bounce_back_status"],
                [
                    ["UC001", "Alpha", "PEGA", "11", "streamA", "Y", "BOUNCE_X"],
                    ["UC002", "Beta", "PEGA", "33", "streamB", "Y", "BOUNCE_Y"],
                    ["UC003", "Gamma", "MDC", "37", "streamC", "N", "BOUNCE_Z"],
                ],
            )
            dataset_dir = _write_manifest_dataset(tmp, environment="UAT", master=master)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                report = uc.quality_report()

        self.assertTrue(report["available"])
        self.assertEqual(report["source"]["environment"], "UAT")
        self.assertEqual(report["column_bindings"]["bound"]["status"], "status")
        self.assertEqual(report["column_bindings"]["ambiguous"], {})
        self.assertIn("33", report["illegal_enum"]["codes"])
        self.assertIn("37", report["illegal_enum"]["codes"])
        self.assertEqual(report["active_inactive"], {
            "active": 2, "inactive": 1,
            "examples": {"active": ["UC001", "UC002"], "inactive": ["UC003"]},
        })
        self.assertFalse(report["route_dimension"]["available"])
        self.assertEqual(report["route_dimension"]["reason"], "no same-environment route snapshot")
        markdown = uc.render_quality_markdown(report)
        self.assertIn("Coverage funnel", markdown)
        self.assertIn("Route dimension", markdown)


if __name__ == "__main__":
    unittest.main()
