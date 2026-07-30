"""`tbl_use_case_router` — the authoritative carrier table, joined on its real four-column key.

The intranet reported the key as `business_category + channel + route + router` (which refutes
RUNBOOK-54 question 1's `channel_rule.route = router.id` guess) along with three numbers that shape
every assertion here: 247 rows, ~49.8% of child rows back-link, 58.7% of router rows have a blank
vendor, and the only four vendor values present are `AWS HK SNS` / `AWS SG SNS` / `HTCL` /
`HTCL OLD` — display names, not tokens.

Most of these tests are about REFUSING: a partial key must not match, an unconfirmed display name
must not be translated, and a blank vendor must not read as "no carrier".
"""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

from retriever import config as rconfig
from retriever import delivery_chain, usecase_router

ROUTER_HEADER = ["id", "business_category", "channel", "route", "router", "vendor",
                 "message_process_sla", "message_delivery_sla", "delivery_path"]
ROUTER_ROWS = [
    # The four real vendor shapes plus a blank one (58.7% of the table).
    ["1", "11", "SMS", "HUTCHISON_GW_SMS", "R_HTCL", "HTCL", "5", "30", "2"],
    ["2", "11", "SMS", "LEGACY_GW_SMS", "R_HTCL_OLD", "HTCL OLD", "5", "30", "2"],
    ["3", "20", "PUSH", "SNS_HK", "R_SNS_HK", "AWS HK SNS", "3", "10", "1"],
    ["4", "20", "PUSH", "SNS_SG", "R_SNS_SG", "AWS SG SNS", "3", "10", "1"],
    ["5", "11", "EMAIL", "PFP_BULK", "R_PFP", "", "9", "60", "4"],
]


def _write_dataset(base_dir, router_rows=ROUTER_ROWS):
    dataset_dir = os.path.join(base_dir, "index", "usecase-snapshots", "active")
    os.makedirs(dataset_dir, exist_ok=True)
    path = os.path.join(dataset_dir, "tbl_use_case_router.snapshot.csv")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(ROUTER_HEADER) + "\n")
        for row in router_rows:
            handle.write(",".join(row) + "\n")
    with open(os.path.join(dataset_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump({"environment": "UAT", "snapshot_id": "20260727-1542", "tables": {
            "tbl_use_case_router": {"file": "tbl_use_case_router.snapshot.csv",
                                     "row_count": len(router_rows)}}}, handle)
    return dataset_dir


class _DatasetMixin:
    def _with_dataset(self, stack, router_rows=ROUTER_ROWS, columns_config=None):
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        dataset_dir = _write_dataset(tmp, router_rows)
        stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
        stack.enter_context(mock.patch.object(rconfig, "USECASE_DATASET_DIR", dataset_dir))
        columns_path = os.path.join(tmp, "usecase_columns.json")
        if columns_config is not None:
            with open(columns_path, "w", encoding="utf-8") as handle:
                json.dump(columns_config, handle)
        stack.enter_context(mock.patch.object(rconfig, "USECASE_COLUMNS_JSON", columns_path))
        return tmp


class NaturalKeyTests(_DatasetMixin, unittest.TestCase):
    def test_default_key_is_the_four_columns_the_intranet_reported(self):
        self.assertEqual(usecase_router.DEFAULT_NATURAL_KEY,
                         ("business_category", "channel", "route", "router"))

    def test_intranet_knob_overrides_the_default(self):
        with ExitStack() as stack:
            self._with_dataset(stack, columns_config={
                "validation": {"router_natural_key": ["channel", "route"]}})
            self.assertEqual(usecase_router.natural_key_fields(), ("channel", "route"))

    def test_join_resolves_on_the_full_four_column_key(self):
        rule = {"channel": "SMS", "route": "HUTCHISON_GW_SMS", "router": "R_HTCL",
                "citation": "rule.csv:2"}
        with ExitStack() as stack:
            self._with_dataset(stack)
            match = usecase_router.router_for_rule(rule, business_category="11")
        self.assertTrue(match["matched"])
        self.assertEqual(match["router_id"], "1")
        self.assertEqual(match["delivery_path"], "2")
        self.assertEqual(match["message_process_sla"], "5")
        self.assertTrue(match["citation"])

    def test_a_blank_key_component_is_a_reported_miss_not_a_partial_match(self):
        """The ~50% that don't back-link. A partial key would land on another carrier's row."""
        rule = {"channel": "SMS", "route": "HUTCHISON_GW_SMS", "router": ""}
        with ExitStack() as stack:
            self._with_dataset(stack)
            match = usecase_router.router_for_rule(rule, business_category="11")
        self.assertFalse(match["matched"])
        self.assertEqual(match["missing_key_fields"], ["router"])
        self.assertIn("partial key is never matched", match["reason"])

    def test_missing_business_category_blocks_the_join_even_with_perfect_rules(self):
        """business_category lives on the MASTER row, so a route-snapshot-only use case cannot
        reach its authoritative carrier however complete its channel rules are."""
        rule = {"channel": "SMS", "route": "HUTCHISON_GW_SMS", "router": "R_HTCL"}
        with ExitStack() as stack:
            self._with_dataset(stack)
            match = usecase_router.router_for_rule(rule, business_category="")
        self.assertFalse(match["matched"])
        self.assertEqual(match["missing_key_fields"], ["business_category"])

    def test_rule_level_business_category_wins_over_the_master(self):
        rule = {"channel": "PUSH", "route": "SNS_HK", "router": "R_SNS_HK",
                "business_category": "20"}
        with ExitStack() as stack:
            self._with_dataset(stack)
            match = usecase_router.router_for_rule(rule, business_category="11")
        self.assertTrue(match["matched"])
        self.assertEqual(match["router_id"], "3")

    def test_wrong_business_category_does_not_match(self):
        rule = {"channel": "SMS", "route": "HUTCHISON_GW_SMS", "router": "R_HTCL"}
        with ExitStack() as stack:
            self._with_dataset(stack)
            match = usecase_router.router_for_rule(rule, business_category="99")
        self.assertFalse(match["matched"])
        self.assertIn("no tbl_use_case_router row", match["reason"])
        self.assertIn("mixed_export_times", match["reason"])

    def test_duplicate_natural_key_refuses_to_pick_one(self):
        """The intranet reported the key as unique; a duplicate is a contract change to surface."""
        rows = ROUTER_ROWS + [["6", "11", "SMS", "HUTCHISON_GW_SMS", "R_HTCL", "HTCL", "5", "30", "2"]]
        rule = {"channel": "SMS", "route": "HUTCHISON_GW_SMS", "router": "R_HTCL"}
        with ExitStack() as stack:
            self._with_dataset(stack, router_rows=rows)
            match = usecase_router.router_for_rule(rule, business_category="11")
        self.assertFalse(match["matched"])
        self.assertIn("refusing to pick one", match["reason"])
        self.assertEqual(len(match["ambiguous_citations"]), 2)

    def test_absent_table_says_absent_not_columns_unbound(self):
        with ExitStack() as stack:
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(mock.patch.object(rconfig, "ROOT", tmp))
            stack.enter_context(mock.patch.object(
                rconfig, "USECASE_DATASET_DIR", os.path.join(tmp, "absent")))
            stack.enter_context(mock.patch.object(
                rconfig, "USECASE_COLUMNS_JSON", os.path.join(tmp, "absent.json")))
            match = usecase_router.router_for_rule({"channel": "SMS"}, "11")
        self.assertFalse(match["matched"])
        self.assertIn("not in the active dataset", match["reason"])


class VendorResolutionTests(_DatasetMixin, unittest.TestCase):
    def test_no_display_alias_is_seeded_so_nothing_is_ever_guessed(self):
        """`htcl -> 3hk` is owner-confirmed for REPO NAMES (RUNBOOK-49); whether the router table's
        `HTCL` denotes the same carrier is a different question the intranet explicitly refused to
        assume. Do not 'fix' this by seeding the map."""
        self.assertEqual(usecase_router.DEFAULT_VENDOR_DISPLAY_ALIASES, {})

    def test_unconfirmed_display_name_is_reported_raw_never_translated(self):
        with ExitStack() as stack:
            self._with_dataset(stack)
            for raw in ("HTCL", "HTCL OLD", "AWS HK SNS", "AWS SG SNS"):
                vendor = usecase_router.resolve_vendor(raw)
                self.assertEqual(vendor["raw"], raw)
                self.assertIsNone(vendor["token"], raw)
                self.assertFalse(vendor["confirmed"])
                self.assertTrue(vendor["present"])
                self.assertIn("Do NOT translate it yourself", vendor["note"])

    def test_the_repo_name_parser_would_collapse_the_aws_regions_which_is_why_it_is_not_used(self):
        """Guard on the hazard itself: vendors.pick_vendor takes the RIGHTMOST known token, so both
        AWS rows would become `sns` and the region the authoritative table records would be lost."""
        from retriever.vendors import pick_vendor
        self.assertEqual(pick_vendor("AWS HK SNS".lower().split()), "sns")
        self.assertEqual(pick_vendor("AWS SG SNS".lower().split()), "sns")
        # And the module under test must not do that.
        self.assertIsNone(usecase_router.resolve_vendor("AWS HK SNS")["token"])

    def test_blank_vendor_is_not_recorded_not_no_carrier(self):
        vendor = usecase_router.resolve_vendor("")
        self.assertFalse(vendor["present"])
        self.assertIsNone(vendor["token"])
        self.assertIn("not the same as 'no carrier'", vendor["note"])

    def test_owner_confirmed_alias_resolves_once_the_intranet_supplies_it(self):
        """The seam: one config edit, zero code change."""
        with ExitStack() as stack:
            self._with_dataset(stack, columns_config={
                "validation": {"vendor_display_aliases": {"HTCL": "3hk", "_README": "docs"}}})
            vendor = usecase_router.resolve_vendor("htcl")
            self.assertEqual(vendor["token"], "3hk")
            self.assertTrue(vendor["confirmed"])
            self.assertNotIn("_readme", usecase_router.vendor_display_aliases())


class DeliveryChainTierTests(_DatasetMixin, unittest.TestCase):
    """How the authoritative table composes with the diagram walk."""

    def _rule(self, **kw):
        base = {"channel": "SMS", "route": "HUTCHISON_GW_SMS", "router": "R_HTCL",
                "citation": "rule.csv:2"}
        base.update(kw)
        return base

    def test_matched_row_with_unconfirmed_alias_gets_its_own_tier(self):
        """Neither 'we know' nor 'we have nothing': quote the raw value, keep the path wide."""
        with ExitStack() as stack:
            self._with_dataset(stack)
            result = delivery_chain.exit_path(["SMS"], rules=[self._rule()],
                                              business_category="11")
        selection = result["by_channel"][0]["vendor_selection"]
        self.assertEqual(selection["method"], "router_table_unconfirmed_alias")
        self.assertEqual(selection["authoritative_vendor_raw"], ["HTCL"])
        self.assertTrue(selection["citations"])
        self.assertIn("UPPER BOUND", selection["caveat"])

    def test_confirmed_alias_promotes_to_router_table_and_narrows_the_path(self):
        with ExitStack() as stack:
            self._with_dataset(stack, columns_config={
                "validation": {"vendor_display_aliases": {"HTCL": "3hk"}}})
            result = delivery_chain.exit_path(["SMS"], rules=[self._rule()],
                                              business_category="11")
        channel = result["by_channel"][0]
        self.assertEqual(channel["vendor_selection"]["method"], "router_table")
        self.assertEqual(channel["vendors"], ["3hk"])
        self.assertEqual(channel["terminals"], ["3HK SMSC"])

    def test_router_table_beats_a_conflicting_route_hint(self):
        """A route value naming CSL while the authoritative row says HTCL: the authoritative row
        wins the TIER, and because its alias is unconfirmed the path is not narrowed to CSL."""
        rule = self._rule(route="HUTCHISON_GW_SMS", sender="CSL_GW")
        with ExitStack() as stack:
            self._with_dataset(stack)
            result = delivery_chain.exit_path(["SMS"], rules=[rule], business_category="11")
        selection = result["by_channel"][0]["vendor_selection"]
        self.assertEqual(selection["method"], "router_table_unconfirmed_alias")
        self.assertEqual(selection["authoritative_vendor_raw"], ["HTCL"])

    def test_blank_vendor_row_falls_down_the_ladder_but_still_reports_the_match(self):
        """A matched row with a blank vendor (58.7% of the table) cannot answer 'which carrier', so
        the ladder continues to the next tier — here `route_hint`, because `PFP_BULK` names pfp.
        The match itself is still reported: "we looked, the authoritative row exists, it is silent"
        is different information from "we never looked"."""
        rule = self._rule(channel="EMAIL", route="PFP_BULK", router="R_PFP")
        with ExitStack() as stack:
            self._with_dataset(stack)
            result = delivery_chain.exit_path(["EMAIL"], rules=[rule], business_category="11")
        selection = result["by_channel"][0]["vendor_selection"]
        self.assertEqual(selection["method"], "route_hint")
        self.assertNotIn("authoritative_vendor_raw", selection)
        self.assertTrue(selection["authoritative_lookup"]["matched"])
        self.assertIn("blank", " ".join(selection["authoritative_lookup"]["reasons"]))

    def test_blank_vendor_with_no_route_hint_lands_on_the_upper_bound(self):
        rows = [["9", "11", "EMAIL", "GENERIC_ROUTE", "R_GEN", "", "9", "60", "4"]]
        rule = self._rule(channel="EMAIL", route="GENERIC_ROUTE", router="R_GEN")
        with ExitStack() as stack:
            self._with_dataset(stack, router_rows=rows)
            result = delivery_chain.exit_path(["EMAIL"], rules=[rule], business_category="11")
        selection = result["by_channel"][0]["vendor_selection"]
        self.assertEqual(selection["method"], "channel_upper_bound")
        self.assertTrue(selection["authoritative_lookup"]["matched"])

    def test_failed_lookup_reason_travels_into_the_payload(self):
        with ExitStack() as stack:
            self._with_dataset(stack)
            result = delivery_chain.exit_path(["SMS"], rules=[self._rule(router="")],
                                              business_category="11")
        lookup = result["by_channel"][0]["vendor_selection"]["authoritative_lookup"]
        self.assertFalse(lookup["matched"])
        self.assertTrue(any("incomplete natural key" in reason for reason in lookup["reasons"]))

    def test_coverage_envelope_is_attached_to_every_exit_path(self):
        with ExitStack() as stack:
            self._with_dataset(stack)
            result = delivery_chain.exit_path(["SMS"], rules=[self._rule()],
                                              business_category="11")
        table = result["authoritative_table"]
        self.assertTrue(table["available"])
        self.assertEqual(table["row_count"], 5)
        self.assertEqual(table["key_fields"], ["business_category", "channel", "route", "router"])
        self.assertIn("QUARTER", table["coverage_note"])


if __name__ == "__main__":
    unittest.main()
