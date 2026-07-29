"""Two gaps found while asking a real question ("show me M2050's full configuration and channels")
that the assistant answered with a vague 无法确认:

1. A missing master row was SILENT. `if master:` skipped identity/governance/channels and the
   report carried no trace of which dataset had been searched, so "dataset not configured on this
   box" and "this id lives in the other environment's snapshot" produced identical output — and
   both read like "this use case has no channels or owner".
2. The chain stopped at ingress. No carrier, no exit, nothing about what the customer receives.
"""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

import impact_report
from retriever import config as rconfig

MASTER_HEADER = (
    "use_case_id,use_case_name,project_name,source_system,work_stream_name,line_of_business,"
    "business_category,country_code,group_member,app_name,created_by,created_time,modified_by,"
    "last_modified_time,status\n"
)
MASTER_ROWS = [
    "UC001,Alpha Case,ProjA,PEGA,streamA,WPB,11,HK,HASE,appA,alice,2020-01-01,alice,2020-01-01,Y\n",
]
RULE_HEADER = "use_case_id,channel,priority,route,router,traffic_percentage,tag,sender,send_policy,status\n"
RULE_ROWS = ["UC001,SMS,1,CSL_SVC_RT_SMS,,100,T,SYS,IMMEDIATE,Y\n"]
# M2050 is in the ROUTE snapshot only — the real-world case: 353 dev/SCT route ids vs 2,810 UAT
# master ids, intersection 297.
ROUTING = "use_case_id,topic\nUC001,alerts.sms.topic\nM2050,hrn.hase.wpb.sms.highrisk\n"
MESSAGES = (
    "producer_repo,destination,consumer_repo,routing_source,evidence\n"
    "svc-a,alerts.sms.topic,svc-b,annotation,src/A.java:1\n"
    "svc-a,hrn.hase.wpb.sms.highrisk,mc-hk-hase-tracking-job,annotation,src/B.java:1\n"
)
REPO_TAGS = {
    "svc-a": {"system": "hase", "channel": ["sms"], "mode": "api", "tokens": [], "bundle": "b"},
    "svc-b": {"system": "hase", "channel": ["sms"], "mode": "job", "tokens": [], "bundle": "b"},
    "mc-hk-hase-tracking-job": {"system": "hase", "channel": ["sms"], "mode": "job",
                                 "tokens": [], "bundle": "b"},
}
TOPOLOGY = {
    "sms": {
        "csl": {"delivery_jobs": [{"repo": "mc-hk-hase-csl-sms-deli-job"}],
                 "outbound_apis": [{"repo": "mc-hk-hase-csl-outbound-api"}]},
        "3hk": {"delivery_jobs": [{"repo": "mc-hk-hase-htcl-sms-deli-job"}],
                 "outbound_apis": [{"repo": "mc-hk-hase-htcl-outbound-api"}]},
    },
}


class _Fixture:
    def _build(self, stack, with_master=True, with_rules=True):
        root = stack.enter_context(tempfile.TemporaryDirectory())
        recon_dir = os.path.join(root, "recon_out")
        index_dir = os.path.join(root, "index")
        os.makedirs(recon_dir)
        os.makedirs(index_dir)

        def write(path, text):
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)

        write(os.path.join(recon_dir, "internal_edges.csv"), "from_repo,to_repo\n")
        write(os.path.join(index_dir, "message_edges.csv"), MESSAGES)
        write(os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv"), ROUTING)
        master_path = os.path.join(index_dir, "tbl_use_case.snapshot.csv")
        if with_master:
            write(master_path, MASTER_HEADER + "".join(MASTER_ROWS))
        with open(os.path.join(index_dir, "repo_tags.json"), "w", encoding="utf-8") as handle:
            json.dump(REPO_TAGS, handle)
        with open(os.path.join(index_dir, "glossary.json"), "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        topology_path = os.path.join(index_dir, "delivery_topology.json")
        with open(topology_path, "w", encoding="utf-8") as handle:
            json.dump(TOPOLOGY, handle)

        dataset_dir = os.path.join(index_dir, "usecase-snapshots", "active")
        if with_rules:
            # Manifest dataset — the only mode that carries channel rules (legacy is one table).
            os.makedirs(dataset_dir)
            write(os.path.join(dataset_dir, "tbl_use_case.snapshot.csv"),
                  MASTER_HEADER + "".join(MASTER_ROWS))
            write(os.path.join(dataset_dir, "tbl_use_case_channel_rule.snapshot.csv"),
                  RULE_HEADER + "".join(RULE_ROWS))
            write(os.path.join(dataset_dir, "tbl_event_router_usecase_topic.snapshot.csv"), ROUTING)
            with open(os.path.join(dataset_dir, "manifest.json"), "w", encoding="utf-8") as handle:
                json.dump({"environment": "UAT", "snapshot_id": "20260729", "tables": {
                    "tbl_use_case": {"file": "tbl_use_case.snapshot.csv", "row_count": 1},
                    "tbl_use_case_channel_rule": {
                        "file": "tbl_use_case_channel_rule.snapshot.csv", "row_count": 1},
                    "tbl_event_router_usecase_topic": {
                        "file": "tbl_event_router_usecase_topic.snapshot.csv", "row_count": 2},
                }}, handle)

        patch = lambda name, value: stack.enter_context(mock.patch.object(rconfig, name, value))
        patch("ROOT", root)
        patch("INDEX_DIR", index_dir)
        patch("RECON_DIR", recon_dir)
        patch("EDGES_CSV", os.path.join(recon_dir, "internal_edges.csv"))
        patch("MESSAGE_EDGES_CSV", os.path.join(index_dir, "message_edges.csv"))
        patch("USECASE_SNAPSHOT_CSV",
              os.path.join(index_dir, "tbl_event_router_usecase_topic.snapshot.csv"))
        patch("USECASE_DATASET_DIR", dataset_dir if with_rules else os.path.join(root, "absent"))
        patch("USECASE_MASTER_CSV", master_path if with_master else os.path.join(root, "absent.csv"))
        patch("SOURCE_SYSTEM_ALIASES_JSON", os.path.join(root, "absent-aliases.json"))
        patch("REPO_TAGS_JSON", os.path.join(index_dir, "repo_tags.json"))
        patch("GLOSSARY_JSON", os.path.join(index_dir, "glossary.json"))
        patch("DELIVERY_TOPOLOGY_JSON", topology_path)
        return root


class MasterLookupProvenanceTests(_Fixture, unittest.TestCase):
    def test_route_only_use_case_says_which_half_is_missing_and_why(self):
        """The M2050 case. Before: master silently skipped, answer 无法确认 with no reason."""
        with ExitStack() as stack:
            self._build(stack)
            report = impact_report.build_report("use-case:M2050")
        provenance = report["use_case_master"]
        self.assertFalse(provenance["found"])
        self.assertTrue(provenance["dataset_available"])
        self.assertEqual(provenance["source"]["environment"], "UAT")
        self.assertIn("M2050", provenance["note"])
        self.assertIn("different populations", provenance["note"])
        self.assertIn("its routing is real", provenance["note"])
        self.assertIn(provenance["note"],
                      [callout["message"] for callout in report["risk_callouts"]])

    def test_absent_dataset_is_named_as_an_environment_gap_not_a_fact_about_the_use_case(self):
        with ExitStack() as stack:
            self._build(stack, with_master=False, with_rules=False)
            report = impact_report.build_report("use-case:UC001")
        provenance = report["use_case_master"]
        self.assertFalse(provenance["dataset_available"])
        self.assertIn("NOT configured", provenance["note"])
        self.assertIn("environment gap", provenance["note"])

    def test_found_use_case_carries_provenance_without_a_complaint(self):
        with ExitStack() as stack:
            self._build(stack)
            report = impact_report.build_report("use-case:UC001")
        provenance = report["use_case_master"]
        self.assertTrue(provenance["found"])
        self.assertTrue(provenance["has_channel_rules"])
        self.assertNotIn("note", provenance)
        self.assertEqual(provenance["source"]["snapshot_id"], "20260729")


class DeliveryChainInReportTests(_Fixture, unittest.TestCase):
    def test_declared_channels_drive_a_chain_that_ends_at_the_carrier(self):
        with ExitStack() as stack:
            self._build(stack)
            report = impact_report.build_report("use-case:UC001")
        chain = report["delivery_chain"]
        self.assertTrue(chain["available"])
        self.assertEqual(chain["channel_source"], "declared")
        # The rule's route names CSL, so the carrier is narrowed — and still labelled a hint.
        self.assertEqual(chain["vendors"], ["csl"])
        self.assertEqual(chain["terminals"], ["CSL SMSC"])
        self.assertEqual(chain["by_channel"][0]["vendor_selection"]["method"], "route_hint")

    def test_route_only_use_case_still_gets_an_exit_path_but_flagged_inferred(self):
        """M2050 has no channel rule, so its channels come from repo tags. That is a weaker basis
        than a declared channel and the payload has to say so — but stopping at ingress, which is
        what used to happen, told the reader nothing at all."""
        with ExitStack() as stack:
            self._build(stack)
            report = impact_report.build_report("use-case:M2050")
        chain = report["delivery_chain"]
        self.assertTrue(chain["available"])
        self.assertEqual(chain["channel_source"], "inferred")
        self.assertIn("CSL SMSC", chain["terminals"])
        self.assertTrue(chain["caveats"][0].startswith("channels here are INFERRED"))
        self.assertEqual(chain["by_channel"][0]["vendor_selection"]["method"],
                         "channel_upper_bound")

    def test_markdown_renders_the_exit_and_the_lookup_note(self):
        with ExitStack() as stack:
            self._build(stack)
            report = impact_report.build_report("use-case:M2050")
            text = impact_report.render_report_markdown(report)
        self.assertIn("## Delivery Chain (to the exit)", text)
        self.assertIn("CSL SMSC", text)
        self.assertIn("## Use Case Master Lookup", text)
        self.assertIn("M2050", text)

    def test_existing_report_sections_are_untouched(self):
        """Additive change: the pre-existing sections must not shift."""
        with ExitStack() as stack:
            self._build(stack)
            report = impact_report.build_report("use-case:UC001")
        for key in ("target", "upstream", "downstream", "async_routes", "channel_chain"):
            self.assertIn(key, report)
        self.assertIn("sms", [item["channel"] for item in report["channel_chain"]])


if __name__ == "__main__":
    unittest.main()
