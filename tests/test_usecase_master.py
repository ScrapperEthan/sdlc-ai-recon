import json
import os
import tempfile
import unittest
from unittest import mock

from retriever import config, usecase_master as um

MASTER_HEADER = (
    "use_case_id,use_case_name,project_name,source_system,work_stream_name,line_of_business,"
    "business_category,country_code,group_member,app_name,created_by,created_time,modified_by,"
    "last_modified_time,status,marketing_optin_flag,sms_optin_flag,push_optin_flag\n"
)

# UC001: PEGA, has a route (see ROUTING below), consent Y on marketing + push only.
# UC002: PEGA (aliased spelling PEGA_HK), no route -> catalog-only.
# UC003: MDC, illegal business_category code 33 (data-contract drift), stale (2019).
ROWS = [
    "UC001,Alpha Case,ProjA,PEGA,streamA,WPB,11,HK,HASE,appA,alice,2020-01-01,alice,2020-01-01,Y,Y,N,Y\n",
    "UC002,Beta Case,ProjB,PEGA_HK,streamB,CMB,6,HK,HSBC,appB,bob,2024-06-01,bob,2024-06-01,Y,N,N,N\n",
    "UC003,Gamma Case,ProjC,MDC,streamC,WPB,33,HK,HASE,appC,carol,2019-01-01,carol,2019-01-01,Y,N,N,N\n",
]

ROUTING = "use_case_id,topic\nUC001,alerts.sms.topic\n"

ALIASES = {"PEGA": ["Pega", "PEGA_HK"]}


class UsecaseMasterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        master_path = os.path.join(root, "tbl_use_case.snapshot.csv")
        with open(master_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(MASTER_HEADER)
            handle.writelines(ROWS)
        routing_path = os.path.join(root, "tbl_event_router_usecase_topic.snapshot.csv")
        with open(routing_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(ROUTING)
        aliases_path = os.path.join(root, "source_system_aliases.json")
        with open(aliases_path, "w", encoding="utf-8") as handle:
            json.dump(ALIASES, handle)

        self._patches = [
            mock.patch.object(config, "ROOT", root),
            # No manifest dir here -> forces active_dataset() to fall through to the legacy
            # USECASE_MASTER_CSV path below. Without this, an ambient SDLC_USECASE_DATASET env
            # var (e.g. on a box with a real UAT dataset configured) would make active_dataset()
            # find that manifest FIRST and this fixture's rows would never be read (RUNBOOK-45
            # Part A follow-up #1 — this is what caused 14/243 to fail against real UAT data).
            mock.patch.object(config, "USECASE_DATASET_DIR", os.path.join(root, "no-manifest-here")),
            mock.patch.object(config, "USECASE_MASTER_CSV", master_path),
            mock.patch.object(config, "USECASE_SNAPSHOT_CSV", routing_path),
            mock.patch.object(config, "SOURCE_SYSTEM_ALIASES_JSON", aliases_path),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_master_for_returns_identity_and_citation(self):
        row = um.master_for("uc001")  # case-insensitive
        self.assertIsNotNone(row)
        self.assertEqual(row["source_system"], "PEGA")
        self.assertEqual(row["name"], "Alpha Case")
        self.assertTrue(row["citation"].endswith(":2"))

    def test_master_for_unknown_id_returns_none(self):
        self.assertIsNone(um.master_for("does-not-exist"))

    def test_use_cases_for_source_system_splits_has_route(self):
        out = um.use_cases_for_source_system("PEGA")
        self.assertTrue(out["available"])
        by_id = {item["use_case_id"]: item for item in out["items"]}
        self.assertTrue(by_id["UC001"]["has_route"])
        self.assertFalse(by_id["UC002"]["has_route"])

    def test_alias_folds_differently_spelled_source_system(self):
        # UC002's raw source_system is "PEGA_HK"; querying "PEGA" must still find it via aliases.
        out = um.use_cases_for_source_system("PEGA")
        ids = {item["use_case_id"] for item in out["items"]}
        self.assertIn("UC002", ids)
        self.assertEqual(out["total"], 2)

    def test_missing_master_file_never_crashes(self):
        with mock.patch.object(config, "USECASE_MASTER_CSV", os.path.join(self._tmp.name, "nope.csv")):
            self.assertIsNone(um.master_for("UC001"))
            out = um.use_cases_for_source_system("PEGA")
            self.assertFalse(out["available"])
            self.assertEqual(out["items"], [])
            self.assertEqual(um.source_systems(), [])
            report = um.quality_report()
            self.assertFalse(report["available"])

    def test_illegal_business_category_code_flagged_unknown(self):
        row = um.master_for("UC003")
        self.assertEqual(row["business_category_label"], "UNKNOWN(33)")
        report = um.quality_report()
        self.assertIn("33", report["illegal_enum"]["codes"])
        self.assertIn("UC003", report["illegal_enum"]["examples"])

    def test_consent_preflight_surfaces_only_y_flags(self):
        out = um.consent_preflight("UC001")
        labels = {check["consent"] for check in out["checks"]}
        self.assertEqual(labels, {"Marketing Consent", "Push"})  # sms_optin_flag was N

    def test_consent_preflight_is_documented_as_not_the_channel_list(self):
        self.assertIn("NOT the channel list", um.consent_preflight.__doc__)

    def test_owners_for_dedupes_across_ids(self):
        # No tbl_use_case_ext in this fixture -> only config_maintainers (created_by/modified_by)
        # populate; business_owners/cost_governance stay empty rather than crashing (defect #7).
        self.assertEqual(
            um.owners_for(["UC001", "UC002"]),
            {"business_owners": [], "cost_governance": [], "config_maintainers": ["alice", "bob"]},
        )

    def test_quality_report_coverage_funnel_and_active_inactive(self):
        # No channel_rule/ext tables in this fixture -> every UC is catalog_only; all 3 rows are
        # status=Y so active=3/inactive=0. Replaces the old join_coverage (routed vs master) block,
        # which computed off the dev/SCT route snapshot regardless of the active dataset's own
        # environment — that shape is gone (Round A coverage funnel replaces it, B10).
        report = um.quality_report()
        self.assertEqual(report["coverage_funnel"]["configured"], 0)
        self.assertEqual(report["coverage_funnel"]["catalog_only"], 3)
        self.assertEqual(report["active_inactive"], {
            "active": 3, "inactive": 0,
            "examples": {"active": ["UC001", "UC002", "UC003"], "inactive": []},
        })

    def test_quality_report_column_bindings_present(self):
        report = um.quality_report()
        self.assertEqual(report["column_bindings"]["bound"]["status"], "status")
        self.assertEqual(report["column_bindings"]["ambiguous"], {})


if __name__ == "__main__":
    unittest.main()
