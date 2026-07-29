"""The last mile: declared channels -> … -> carrier -> exit.

The chain used to stop at ingress/decision, so these tests are mostly about the far end: that a
terminal is actually named, that a narrowed carrier stays narrow all the way down to the repos,
and that every place we DON'T know the carrier says so instead of picking one.
"""
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

from retriever import config as rconfig
from retriever import delivery_chain

TOPOLOGY = {
    "sms": {
        "csl": {"delivery_jobs": [{"repo": "mc-hk-hase-csl-sms-deli-job"}],
                 "outbound_apis": [{"repo": "mc-hk-hase-csl-outbound-api"}]},
        "3hk": {"delivery_jobs": [{"repo": "mc-hk-hase-htcl-sms-deli-job"}],
                 "outbound_apis": [{"repo": "mc-hk-hase-htcl-outbound-api"}]},
        # `cm` is a real carrier in the repo topology that the static diagram never drew — the
        # off-diagram case.
        "cm": {"delivery_jobs": [{"repo": "mc-hk-hase-cm-sms-deli-job"}],
                "outbound_apis": [{"repo": "mc-hk-hase-cm-outbound-api"}]},
        "unknown": {"delivery_jobs": [{"repo": "mc-hk-hase-sms-deli-job"}], "outbound_apis": []},
    },
    "email": {
        "pfp": {"delivery_jobs": [{"repo": "mc-hk-hase-pfp-email-deli-job"}],
                 "outbound_apis": [{"repo": "mc-hk-hase-pfp-outbound-api"}]},
    },
}


class _TopologyMixin:
    def _with_topology(self, stack, payload=TOPOLOGY):
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        path = os.path.join(tmp, "delivery_topology.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        stack.enter_context(mock.patch.object(rconfig, "DELIVERY_TOPOLOGY_JSON", path))
        return path

    def _channel(self, result, name):
        for item in result["by_channel"]:
            if item["channel"] == name:
                return item
        self.fail(f"no {name} channel in {[c['channel'] for c in result['by_channel']]}")


class ChannelNormalizationTests(unittest.TestCase):
    def test_db_channel_values_fold_onto_delivery_channels(self):
        self.assertEqual(delivery_chain.canonical_channel("SMS"), "sms")
        self.assertEqual(delivery_chain.canonical_channel("TWOWAYSMS"), "sms")
        self.assertEqual(delivery_chain.canonical_channel("PUSH+INBOX"), "push")
        self.assertEqual(delivery_chain.canonical_channel("PUSH_INBOX"), "push")
        self.assertEqual(delivery_chain.canonical_channel(" email "), "email")

    def test_unknown_channel_is_blank_not_guessed(self):
        self.assertEqual(delivery_chain.canonical_channel("INAPP"), "")
        self.assertEqual(delivery_chain.canonical_channel(""), "")
        self.assertEqual(delivery_chain.canonical_channel(None), "")


class ExitPathTests(_TopologyMixin, unittest.TestCase):
    def test_chain_reaches_the_carrier_terminal_not_just_ingress(self):
        """The whole point: an SMS use case must come back with an SMSC, not stop at a topic."""
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["SMS"])
        sms = self._channel(result, "sms")
        stages = [stage["stage"] for stage in sms["stages"]]
        self.assertEqual(stages[0], "topic")
        self.assertIn("delivery-job", stages)
        self.assertIn("outbound-api", stages)
        self.assertEqual(stages[-1], "vendor-terminal")
        self.assertIn("CSL SMSC", sms["terminals"])
        self.assertIn("3HK SMSC", sms["terminals"])
        self.assertIn("mc-hk-hase-csl-outbound-api",
                      [repo for stage in sms["stages"] for repo in stage["repos"]])

    def test_without_a_route_hint_the_carrier_set_is_an_explicit_upper_bound(self):
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["SMS"])
        sms = self._channel(result, "sms")
        self.assertEqual(sms["vendor_selection"]["method"], "channel_upper_bound")
        self.assertIn("UPPER BOUND", sms["vendor_selection"]["caveat"])
        self.assertIn("tbl_use_case_router", sms["vendor_selection"]["caveat"])
        self.assertGreater(len(sms["vendors"]), 1)

    def test_route_value_naming_a_carrier_narrows_the_path_down_to_the_repos(self):
        """A narrowed answer must be narrow everywhere — including the shared delivery-job node,
        which has no vendor of its own and would otherwise list every carrier's repos underneath a
        single-carrier heading."""
        rules = [{"channel": "SMS", "route": "CSL_SVC_RT_SMS", "citation": "index/rule.csv:7"}]
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["SMS"], rules=rules)
        sms = self._channel(result, "sms")
        self.assertEqual(sms["vendor_selection"]["method"], "route_hint")
        self.assertEqual(sms["vendors"], ["csl"])
        self.assertEqual(sms["terminals"], ["CSL SMSC"])
        self.assertIn("index/rule.csv:7", sms["vendor_selection"]["citations"])
        repos = {repo for stage in sms["stages"] for repo in stage["repos"]}
        self.assertIn("mc-hk-hase-csl-sms-deli-job", repos)
        self.assertNotIn("mc-hk-hase-htcl-sms-deli-job", repos)
        self.assertNotIn("mc-hk-hase-cm-sms-deli-job", repos)

    def test_narrowing_is_labelled_a_hint_never_a_confirmed_mapping(self):
        rules = [{"channel": "SMS", "router": "CSL_SVC_RT_SMS", "citation": "index/rule.csv:7"}]
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["SMS"], rules=rules)
        caveat = self._channel(result, "sms")["vendor_selection"]["caveat"]
        self.assertIn("HINT", caveat)
        self.assertIn("RUNBOOK-54", caveat)

    def test_carrier_in_the_topology_but_not_on_the_diagram_is_still_reported(self):
        """Under-reporting the exit is the one failure this module exists to prevent."""
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["SMS"])
        sms = self._channel(result, "sms")
        self.assertIn("cm", sms["vendors"])
        self.assertEqual(sms["vendors_off_diagram"], ["cm"])
        off = [s for s in sms["stages"] if s.get("off_diagram")]
        self.assertEqual([s["repos"] for s in off], [["mc-hk-hase-cm-outbound-api"]])
        self.assertTrue(any("not drawn on the static architecture diagram" in c
                            for c in result["caveats"]))

    def test_unknown_vendor_bucket_never_becomes_a_carrier(self):
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["SMS"])
        self.assertNotIn("unknown", self._channel(result, "sms")["vendors"])

    def test_two_way_sms_rides_the_sms_last_mile_under_its_own_declared_name(self):
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["TWOWAYSMS"])
        sms = self._channel(result, "sms")
        self.assertEqual(sms["declared_as"], ["TWOWAYSMS"])
        self.assertIn("CSL SMSC", sms["terminals"])

    def test_unmapped_channel_is_surfaced_not_folded_into_a_neighbour(self):
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["INAPP", "EMAIL"])
        self.assertEqual(result["unmapped_channels"], ["INAPP"])
        self.assertEqual([c["channel"] for c in result["by_channel"]], ["email"])
        self.assertTrue(any("no known exit path" in c for c in result["caveats"]))

    def test_no_declared_channel_says_why_rather_than_returning_an_empty_chain(self):
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path([])
        self.assertFalse(result["available"])
        self.assertIn("no declared channels", result["reason"])

    def test_missing_topology_keeps_the_structure_and_says_repos_are_unbound(self):
        """A box that never ran make_delivery_topology.py must still see the shape of the exit —
        and be told the repos are missing, not shown a chain that quietly has none."""
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                rconfig, "DELIVERY_TOPOLOGY_JSON", os.path.join(tempfile.gettempdir(), "absent.json")))
            result = delivery_chain.exit_path(["SMS"])
        self.assertTrue(result["available"])
        self.assertIn("make_delivery_topology.py", result["note"])
        sms = self._channel(result, "sms")
        self.assertIn("CSL SMSC", sms["terminals"])
        self.assertEqual([repo for stage in sms["stages"] for repo in stage["repos"]], [])

    def test_path_summary_is_one_readable_line_ending_at_the_exit(self):
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(["EMAIL"])
        summary = self._channel(result, "email")["path_summary"]
        self.assertTrue(summary.startswith("EMAIL:"))
        self.assertIn("→", summary)
        self.assertTrue(summary.endswith("ProofPoint"), summary)

    def test_topics_are_bound_to_the_matching_channel_only(self):
        with ExitStack() as stack:
            self._with_topology(stack)
            result = delivery_chain.exit_path(
                ["SMS", "EMAIL"], topics=["hrn.hase.sms.realtime", "hrn.hase.email.batch"])
        sms_topic = self._channel(result, "sms")["stages"][0]
        email_topic = self._channel(result, "email")["stages"][0]
        self.assertEqual(sms_topic["topics"], ["hrn.hase.sms.realtime"])
        self.assertEqual(email_topic["topics"], ["hrn.hase.email.batch"])


class VendorHintTests(unittest.TestCase):
    def test_hints_are_mined_from_route_router_and_sender(self):
        rules = [
            {"channel": "SMS", "route": "CSL_SVC_RT_SMS", "citation": "a:1"},
            {"channel": "SMS", "router": "HUTCHISON", "sender": "3HK_GW", "citation": "a:2"},
            {"channel": "EMAIL", "route": "PFP_BULK", "citation": "a:3"},
        ]
        hints = delivery_chain.vendor_hints(rules)
        self.assertEqual(sorted(hints["sms"]), ["3hk", "csl"])
        self.assertEqual(sorted(hints["email"]), ["pfp"])
        self.assertEqual(hints["sms"]["csl"], ["a:1"])

    def test_a_route_value_naming_no_known_carrier_yields_no_hint(self):
        rules = [{"channel": "SMS", "route": "DEFAULT_ROUTE", "citation": "a:1"}]
        self.assertEqual(delivery_chain.vendor_hints(rules), {})

    def test_rules_on_an_unmapped_channel_do_not_leak_into_another_channel(self):
        rules = [{"channel": "INAPP", "route": "CSL_SVC_RT_SMS", "citation": "a:1"}]
        self.assertEqual(delivery_chain.vendor_hints(rules), {})


if __name__ == "__main__":
    unittest.main()
