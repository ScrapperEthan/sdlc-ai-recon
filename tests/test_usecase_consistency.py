"""Round B3 tests for retriever/usecase_consistency.py — the multi-source consistency validator."""
import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import config, usecase_consistency as consistency


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join(row) + "\n")


def _write_manifest_dataset(base_dir, environment="UAT", snapshot_id="20260722",
                             master=None, rule=None, ext=None, route=None):
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
        header, data_rows = payload
        path = os.path.join(dataset_dir, filename)
        _write_csv(path, header, data_rows)
        tables[name] = {"file": filename, "row_count": len(data_rows)}
    manifest = {"environment": environment, "snapshot_id": snapshot_id,
                "exported_at": "2026-07-22T00:00:00+08:00", "tables": tables}
    with open(os.path.join(dataset_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    return dataset_dir


_RULE_HEADER = ["use_case_id", "channel", "priority", "route", "router",
                "traffic_percentage", "tag", "sender", "send_policy", "status"]


def _rule(uc_id, channel, priority, route="R", router="RT"):
    return [uc_id, channel, priority, route, router, "100", "T", "SYS", "IMMEDIATE", "Y"]


class CheckUseCaseTests(unittest.TestCase):
    def test_canonical_i0141_expression_vs_priority_mismatch(self):
        # RUNBOOK-45 Part B canonical case: rule_text groups EMAIL & SMS, but channel_rule priority
        # is a strict 1<2<3 order across LETTER/EMAIL/SMS.
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["I0141", "Y"]])
            rule = (_RULE_HEADER, [
                _rule("I0141", "LETTER", "1"),
                _rule("I0141", "EMAIL", "2"),
                _rule("I0141", "SMS", "3"),
            ])
            ext = (["use_case_id", "rule_text"], [["I0141", "LETTER > (EMAIL & SMS)"]])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                findings = consistency.check_use_case("I0141")

        checks = {f["check"] for f in findings}
        self.assertIn("expression_vs_priority", checks)
        mismatch = next(f for f in findings if f["check"] == "expression_vs_priority")
        self.assertEqual(mismatch["severity"], "error")
        self.assertTrue(mismatch["citations"])

    def test_channel_set_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["UC001", "Y"]])
            rule = (_RULE_HEADER, [_rule("UC001", "SMS", "1"), _rule("UC001", "PUSH", "2")])
            ext = (["use_case_id", "rule_text"], [["UC001", "SMS > EMAIL"]])  # EMAIL not in rules
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                findings = consistency.check_use_case("UC001")

        checks = {f["check"] for f in findings}
        self.assertIn("channel_set_mismatch", checks)
        # ordering comparison should not ALSO fire once the sets already disagree
        self.assertNotIn("expression_vs_priority", checks)

    def test_matching_expression_and_priority_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["UC002", "Y"]])
            rule = (_RULE_HEADER, [_rule("UC002", "EMAIL", "1"), _rule("UC002", "SMS", "2")])
            ext = (["use_case_id", "rule_text"], [["UC002", "EMAIL > SMS"]])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                findings = consistency.check_use_case("UC002")

        self.assertEqual(findings, [])

    def test_duplicate_and_unknown_channel_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["UC003", "Y"]])
            rule = (_RULE_HEADER, [_rule("UC003", "SMS", "1"), _rule("UC003", "CARRIER_PIGEON", "2")])
            ext = (["use_case_id", "rule_text"], [["UC003", "SMS > CARRIER_PIGEON > SMS"]])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                findings = consistency.check_use_case("UC003")

        checks = {f["check"] for f in findings}
        self.assertIn("duplicate_channel", checks)
        self.assertIn("unknown_channel", checks)

    def test_blank_rule_text_with_rules_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["UC004", "Y"]])
            rule = (_RULE_HEADER, [_rule("UC004", "SMS", "1")])
            ext = (["use_case_id", "rule_text"], [["UC004", ""]])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                findings = consistency.check_use_case("UC004")

        checks = {f["check"] for f in findings}
        self.assertIn("blank_with_rules", checks)

    def test_no_rule_no_ext_returns_empty_not_a_crash(self):
        self.assertEqual(consistency.check_use_case("DOES-NOT-EXIST"), [])
        self.assertEqual(consistency.check_use_case(""), [])
        self.assertEqual(consistency.check_use_case(None), [])


class QualityFindingsTests(unittest.TestCase):
    def test_missing_dataset_is_clean_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "USECASE_DATASET_DIR", os.path.join(tmp, "no-manifest")), \
                 mock.patch.object(config, "USECASE_MASTER_CSV", os.path.join(tmp, "absent.csv")):
                result = consistency.quality_findings()
        self.assertFalse(result["available"])
        self.assertEqual(result["findings"], [])
        self.assertIn("portal_composer_caveat", result)

    def test_folds_in_orphans_no_rule_no_ext_null_priority_and_illegal_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (
                ["use_case_id", "business_category", "status"],
                [
                    ["UC010", "11", "Y"],   # active, will have no channel_rule at all
                    ["UC011", "33", "Y"],   # active, illegal category, has a null-priority rule
                    ["UC012", "11", "N"],   # inactive, no Ext
                ],
            )
            rule = (_RULE_HEADER, [
                _rule("UC011", "SMS", ""),               # null priority
                ["ORPHAN1", "EMAIL", "1", "R", "RT", "100", "T", "SYS", "IMMEDIATE", "Y"],  # orphan
            ])
            ext = (["use_case_id", "rule_text"], [
                ["UC011", "SMS"],
                ["ORPHAN2", "SMS"],  # orphan ext row
            ])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                result = consistency.quality_findings(limit=0)

        self.assertTrue(result["available"])
        checks = {f["check"] for f in result["findings"]}
        self.assertIn("channel_rule_orphan", checks)
        self.assertIn("ext_orphan", checks)
        self.assertIn("active_no_channel_rule", checks)  # UC010
        self.assertIn("null_priority", checks)            # UC011
        self.assertIn("illegal_business_category", checks)  # code 33
        self.assertIn("master_no_ext", checks)

        no_ext_finding = next(f for f in result["findings"] if f["check"] == "master_no_ext")
        # UC010 (active, no ext) + UC012 (inactive, no ext); UC011 has ext.
        self.assertEqual(no_ext_finding["count_active"], 1)
        self.assertEqual(no_ext_finding["count_inactive"], 1)

        uc010_finding = next(f for f in result["findings"]
                              if f["check"] == "active_no_channel_rule" and f["use_case_id"] == "UC010")
        self.assertIn("no ext row", uc010_finding["message"])

    def test_push_inbox_unconfigured_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["A0027", "Y"]])
            rule = (_RULE_HEADER, [
                ["A0027", "PUSH+INBOX", "1", "", "", "100", "T", "SYS", "IMMEDIATE", "Y"],
            ])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                result = consistency.quality_findings(limit=0)

        checks = {f["check"]: f for f in result["findings"]}
        self.assertIn("push_inbox_unconfigured", checks)

    def test_severity_ranking_errors_before_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["UC020", "Y"]])
            rule = (_RULE_HEADER, [_rule("UC020", "SMS", "1"), _rule("UC020", "EMAIL", "2")])
            ext = (["use_case_id", "rule_text"], [["UC020", "SMS & CARRIER_PIGEON"]])  # mismatch (error) + unknown (warning)
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                result = consistency.quality_findings(limit=0)

        severities = [f["severity"] for f in result["findings"]]
        # errors must sort before warnings
        first_warning = next((i for i, s in enumerate(severities) if s == "warning"), len(severities))
        self.assertTrue(all(s == "error" for s in severities[:first_warning]))

    def test_limit_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [[f"UC{i:03d}", "Y"] for i in range(5)]
            master = (["use_case_id", "status"], rows)
            dataset_dir = _write_manifest_dataset(tmp, master=master)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                result = consistency.quality_findings(limit=2)

        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["returned"], 2)
        self.assertTrue(result["truncated"])
        # 5 active_no_channel_rule findings (one per UC) + 1 aggregate master_no_ext finding
        self.assertEqual(result["total_findings"], 6)

    def test_severity_filter_narrows_findings_but_not_counts_by_severity(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = (["use_case_id", "status"], [["UC020", "Y"]])
            rule = (_RULE_HEADER, [_rule("UC020", "SMS", "1"), _rule("UC020", "EMAIL", "2")])
            ext = (["use_case_id", "rule_text"], [["UC020", "SMS & CARRIER_PIGEON"]])
            dataset_dir = _write_manifest_dataset(tmp, master=master, rule=rule, ext=ext)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                warnings_only = consistency.quality_findings(severity="warning", limit=0)
                everything = consistency.quality_findings(limit=0)

        self.assertTrue(all(f["severity"] == "warning" for f in warnings_only["findings"]))
        self.assertLess(warnings_only["total_findings"], everything["total_findings"])
        # counts_by_severity is always the FULL breakdown, unaffected by the severity filter
        self.assertEqual(warnings_only["counts_by_severity"], everything["counts_by_severity"])

    def test_offset_paginates(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [[f"UC{i:03d}", "Y"] for i in range(5)]
            master = (["use_case_id", "status"], rows)
            dataset_dir = _write_manifest_dataset(tmp, master=master)
            with mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir), \
                 mock.patch.object(config, "ROOT", tmp):
                page1 = consistency.quality_findings(offset=0, limit=4)
                page2 = consistency.quality_findings(offset=4, limit=4)

        self.assertEqual(page1["returned"], 4)
        self.assertTrue(page1["truncated"])
        self.assertEqual(page2["returned"], 2)  # 6 total, offset 4 -> 2 remain
        self.assertFalse(page2["truncated"])


if __name__ == "__main__":
    unittest.main()
