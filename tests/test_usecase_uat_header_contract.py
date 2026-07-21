"""UAT header contract test (spec Building block 2 / Tests section): binds correctly against a
REAL-WIDTH header (63 / 42 / 36 columns, like the actual UAT exports) rather than the narrow
hand-picked fixtures used elsewhere. We can't commit the real box CSVs (bank no-egress, no PII/
raw exports in git — see docs/specs/use-case-uat-catalog.md's hard constraints), so this fixture
is a representative reconstruction: every field name Round A actually binds, padded with filler
columns to the documented UAT widths, with the two defect-#3/#4 traps (`unknown_bounce_back_status`
ahead of `status`; singular `marketing_insight_push_optin_flag`) placed exactly as UAT has them.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import config, usecase_catalog as uc

_IDENTITY_NO_STATUS = [
    "use_case_id", "use_case_name", "project_name", "source_system", "work_stream_name",
    "line_of_business", "business_category", "country_code", "group_member", "app_name",
    "created_by", "created_time", "modified_by", "last_modified_time",
]
_CONSENT = [
    "marketing_optin_flag", "push_optin_flag", "marketing_push_optin_flag",
    "high_risk_push_optin_flag", "securities_push_optin_flag",
    "marketing_insight_push_optin_flag",  # UAT: singular "insight" (defect #4)
    "sms_optin_flag", "marketing_sms_optin_flag", "email_optin_flag", "marketing_email_optin_flag",
    "mms_optin_flag", "marketing_mms_optin_flag", "wechat_optin_flag", "marketing_wechat_optin_flag",
    "whatsapp_optin_flag", "marketing_whatsapp_optin_flag",
]

# unknown_bounce_back_status sits BEFORE the real status column, exactly like the real UAT export —
# the old "first column containing the needle" logic would have bound to this one (defect #3).
_MASTER_HEADER = (
    _IDENTITY_NO_STATUS + ["unknown_bounce_back_status", "status"] + _CONSENT
    + [f"filler_master_{i:02d}" for i in range(1, 32)]
)
assert len(_MASTER_HEADER) == 63, len(_MASTER_HEADER)

_RULE_FIXED = ["use_case_id", "channel", "priority", "route", "router", "traffic_percentage",
               "tag", "sender", "send_policy", "status"]
_RULE_HEADER = _RULE_FIXED + [f"filler_rule_{i:02d}" for i in range(1, 33)]
assert len(_RULE_HEADER) == 42, len(_RULE_HEADER)

# dormant_period deliberately absent (Word-doc-only column; schema-drift warning, not a crash).
_EXT_FIXED = [
    "use_case_id", "service_line", "messaging_service_level", "delivery_mode", "endpoint",
    "rule_text", "message_owner", "business_contact", "business_team", "team_head", "depart_head",
    "cost_owner", "signoff_by", "downstream_name", "is_dual_channel", "support_dual_vendor",
    "regulatory_requirement", "high_risk_flag",
]
_EXT_HEADER = _EXT_FIXED + [f"filler_ext_{i:02d}" for i in range(1, 19)]
assert len(_EXT_HEADER) == 36, len(_EXT_HEADER)


def _row(header, overrides):
    return [str(overrides.get(col, "")) for col in header]


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join(row) + "\n")


class UatHeaderContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        dataset_dir = os.path.join(root, "index", "usecase-snapshots", "active")
        os.makedirs(dataset_dir, exist_ok=True)

        master_rows = [
            _row(_MASTER_HEADER, {
                "use_case_id": "M0001", "use_case_name": "Alpha Notification", "source_system": "PEGA",
                "business_category": "11", "status": "Y", "unknown_bounce_back_status": "TRAP_VALUE",
                "marketing_insight_push_optin_flag": "Y", "sms_optin_flag": "N",
            }),
            _row(_MASTER_HEADER, {
                "use_case_id": "M0002", "use_case_name": "Beta Notification", "source_system": "MDC",
                "business_category": "33", "status": "N", "unknown_bounce_back_status": "TRAP_VALUE",
            }),
        ]
        rule_rows = [
            _row(_RULE_HEADER, {
                "use_case_id": "M0001", "channel": "SMS", "priority": "1", "delivery_mode": "REALTIME",
                "status": "Y",
            }),
        ]
        ext_rows = [
            _row(_EXT_HEADER, {
                "use_case_id": "M0001", "delivery_mode": "REALTIME", "endpoint": "mc-hk-hase-ingress-api",
                "rule_text": "LETTER > (EMAIL & SMS)", "message_owner": "Real Business Owner",
            }),
        ]
        _write_csv(os.path.join(dataset_dir, "tbl_use_case.snapshot.csv"), _MASTER_HEADER, master_rows)
        _write_csv(os.path.join(dataset_dir, "tbl_use_case_channel_rule.snapshot.csv"), _RULE_HEADER, rule_rows)
        _write_csv(os.path.join(dataset_dir, "tbl_use_case_ext.snapshot.csv"), _EXT_HEADER, ext_rows)
        manifest = {
            "environment": "UAT", "snapshot_id": "20260720-1730",
            "exported_at": "2026-07-20T17:30:00+08:00",
            "tables": {
                "tbl_use_case": {"file": "tbl_use_case.snapshot.csv", "row_count": len(master_rows)},
                "tbl_use_case_channel_rule": {
                    "file": "tbl_use_case_channel_rule.snapshot.csv", "row_count": len(rule_rows)},
                "tbl_use_case_ext": {"file": "tbl_use_case_ext.snapshot.csv", "row_count": len(ext_rows)},
            },
        }
        with open(os.path.join(dataset_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)

        self._patches = [
            mock.patch.object(config, "USECASE_DATASET_DIR", dataset_dir),
            mock.patch.object(config, "ROOT", root),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_status_binds_to_status_not_the_bounce_back_trap(self):
        manifest = uc.snapshot_manifest()
        self.assertEqual(manifest["environment"], "UAT")
        self.assertEqual(manifest["column_bindings"]["bound"]["status"], "status")
        row = uc.master_for("M0001")
        self.assertEqual(row["status"], "Y")
        row2 = uc.master_for("M0002")
        self.assertEqual(row2["status"], "N")

    def test_singular_marketing_insight_consent_detected(self):
        out = uc.consent_preflight("M0001")
        labels = {check["consent"] for check in out["checks"]}
        self.assertIn("Marketing Insight Push", labels)

    def test_illegal_business_category_33_flagged_on_wide_header(self):
        report = uc.quality_report()
        self.assertIn("33", report["illegal_enum"]["codes"])

    def test_delivery_mode_is_kept_as_a_string_not_coerced(self):
        ext = uc.ext_by_use_case_id()["m0001"]
        self.assertEqual(ext["delivery_mode"], "REALTIME")
        self.assertIsInstance(ext["delivery_mode"], str)

    def test_rule_text_stored_raw_not_parsed(self):
        ext = uc.ext_by_use_case_id()["m0001"]
        self.assertEqual(ext["rule_text"], "LETTER > (EMAIL & SMS)")

    def test_endpoint_resolves_against_repo_universe(self):
        with tempfile.TemporaryDirectory() as repo_tmp:
            repos_txt = os.path.join(repo_tmp, "repos.txt")
            with open(repos_txt, "w", encoding="utf-8") as handle:
                handle.write("mc-hk-hase-ingress-api\n")
            with mock.patch.object(config, "REPOS_TXT", repos_txt):
                segs = uc.resolve_endpoint(uc.ext_by_use_case_id()["m0001"]["endpoint"])
        self.assertEqual(segs[0]["confidence"], "declared-exact")
        self.assertEqual(segs[0]["repo"], "mc-hk-hase-ingress-api")

    def test_active_use_case_is_configured_via_channel_rule(self):
        out = uc.use_cases_for_source_system("PEGA")
        item = next(i for i in out["items"] if i["use_case_id"] == "M0001")
        self.assertTrue(item["configured"])
        self.assertEqual(item["channels"], ["SMS"])

    def test_disabled_use_case_excluded_by_default_included_when_asked(self):
        default_out = uc.use_cases_for_source_system("MDC")
        self.assertEqual(default_out["items"], [])  # M0002 is status=N
        self.assertEqual(default_out["inactive_count"], 1)
        all_out = uc.use_cases_for_source_system("MDC", include_inactive=True)
        self.assertEqual(len(all_out["items"]), 1)
        self.assertFalse(all_out["items"][0]["active"])


if __name__ == "__main__":
    unittest.main()
