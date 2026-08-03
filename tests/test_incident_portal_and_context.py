"""The third branch (Portal delivery records) and the CloudWatch context calls.

Built from `THREE-MCP-EXTERNAL-ENGINE-HANDOFF-20260803.md`: 15 abstract MCP operations were mapped
but only 5 had a caller, so ten were dead wiring. Portal is the P0 because
`MDC Alert - General SHP API Error` — one of the largest alert families here — carries no
CloudWatch alarm and no resolvable LogDream app, which made it un-investigable by every branch that
existed.

The load-bearing tests are the leak tests. A Portal forward record is the most PII-dense thing this
module can touch (recipient, payload, template, message body), so "the real tracking id and the raw
record cannot be found anywhere in what comes back" is the property that matters — not "we wrote
careful extraction code".
"""
import json
import unittest
from unittest import mock

from retriever import incident
from webapp import incident_investigator as inv, mcp_client, mcp_registry

TRACKING = "MDCTRACK-9F2K-88H1"
ALERT_WITH_ID = "MDC Alert - General SHP API Error\ntrackingId: %s\nseverity: MINOR" % TRACKING
# A forward record shaped the way a delivery record plausibly is — every field here except the
# status/reason is something that must NOT survive into the packet.
FORWARD_RECORD = {
    "trackingId": TRACKING,
    "deliveryStatus": "FAILED",
    "failureReason": "vendor gateway timeout after 30000ms",
    "recipient": "9123 4567",
    "email": "alice.wong@example.com",
    "payloadUuid": "8f3a0001-bb21-4c11-9f2a-0d1e2f3a4b5c",
    "messageContent": "Dear customer, your payment of HKD 1,234.00 ...",
    "sentTime": "2026-07-30T03:15:01Z",
}
SECRETS = ("9123 4567", "alice.wong@example.com", "8f3a0001-bb21-4c11-9f2a-0d1e2f3a4b5c",
           "Dear customer", TRACKING)


class TrackingIdExtractionTests(unittest.TestCase):
    """Strict, labelled, local. A loose regex would read a phone number or a payload UUID as a
    tracking id, and Portal would then be queried for a different customer's record."""

    def test_a_single_labelled_id_is_read(self):
        for label in ("trackId", "trackingId", "tracking_id", "mdc_tracking_id"):
            with self.subTest(label=label):
                self.assertEqual(incident.extract_tracking_id(f"{label}: {TRACKING}"), TRACKING)

    def test_json_alerts_answer_for_themselves(self):
        self.assertEqual(
            incident.extract_tracking_id(json.dumps({"trackingId": TRACKING, "sev": "MINOR"})),
            TRACKING)

    def test_two_different_ids_refuse_rather_than_pick_one(self):
        text = f"trackingId: {TRACKING}\ntrackId: OTHER-1234-ZZ"
        self.assertEqual(incident.extract_tracking_id(text), "")

    def test_the_same_id_twice_is_still_unique(self):
        self.assertEqual(
            incident.extract_tracking_id(f"trackId: {TRACKING}\ntrackingId: {TRACKING}"), TRACKING)

    def test_an_unlabelled_token_is_never_read_as_a_tracking_id(self):
        for text in ("9123 4567", "8f3a0001-bb21-4c11-9f2a-0d1e2f3a4b5c",
                     "reference ABCDEF123456", "msgId=CSL100001"):
            with self.subTest(text=text):
                self.assertEqual(incident.extract_tracking_id(text), "")

    def test_length_and_charset_are_bounded(self):
        self.assertEqual(incident.valid_tracking_id("ab"), "")                    # too short
        self.assertEqual(incident.valid_tracking_id("x" * 200), "")               # too long
        self.assertEqual(incident.valid_tracking_id("has space here"), "")
        self.assertEqual(incident.valid_tracking_id("two\nlines"), "")
        self.assertEqual(incident.valid_tracking_id("bad/char$"), "")
        self.assertEqual(incident.valid_tracking_id(f'"{TRACKING}"'), TRACKING)   # quotes stripped


class PortalPlanTests(unittest.TestCase):
    def _plan(self, alert=ALERT_WITH_ID, **kwargs):
        return inv.plan(alert, **kwargs)

    def test_a_caller_supplied_id_beats_the_alert_text(self):
        out = self._plan(tracking_id="CALLER-SUPPLIED-99")
        self.assertEqual(out["portal"]["tracking_id"], "CALLER-SUPPLIED-99")
        self.assertIn("caller", out["portal"]["tracking_id_source"])

    def test_an_id_in_the_alert_is_used_when_the_caller_gives_none(self):
        out = self._plan()
        self.assertEqual(out["portal"]["tracking_id"], TRACKING)
        self.assertTrue(out["portal"]["runnable"])

    def test_no_id_anywhere_refuses_and_says_it_will_not_guess(self):
        out = self._plan("MDC Alert - General SHP API Error")
        self.assertFalse(out["portal"]["runnable"])
        joined = " ".join(out["portal"]["refusals"])
        self.assertIn("NOT guessed", joined)

    def test_an_unusable_supplied_id_refuses_rather_than_falling_back(self):
        out = self._plan("no id here", tracking_id="bad id with spaces")
        self.assertFalse(out["portal"]["runnable"])
        self.assertTrue(any("not usable" in r for r in out["portal"]["refusals"]))

    def test_an_unknown_channel_is_refused_not_defaulted(self):
        out = self._plan(portal_channel="carrier-pigeon")
        self.assertFalse(out["portal"]["runnable"])
        self.assertTrue(any("not one of sms/email/auto" in r for r in out["portal"]["refusals"]))

    def test_portal_runs_on_a_tracking_id_alone(self):
        """The whole point of the branch: no repo, no alarm name, no time window."""
        out = self._plan("something opaque", tracking_id=TRACKING)
        self.assertFalse(out["ok"])                      # log branch cannot run
        self.assertFalse(out["cloudwatch"]["runnable"])  # metric branch cannot run
        self.assertTrue(out["portal"]["runnable"])
        self.assertTrue(out["any_runnable"])             # ...and the investigation still happens


class PortalBranchTests(unittest.TestCase):
    def setUp(self):
        self.enabled = mock.patch.object(inv.config, "MCP_ENABLED", True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def _run(self, responder, alert=ALERT_WITH_ID, **kwargs):
        self.calls = []

        def _call(operation, args=None, **_kw):
            self.calls.append((operation, args or {}))
            return responder(operation, args or {})

        with mock.patch.object(mcp_client, "call", _call):
            events = list(inv.investigate_events(alert, **kwargs))
        packet = [e for e in events if e.get("type") == "result"][0]["packet"]
        return packet, events

    @staticmethod
    def _found(operation, _args):
        if operation.startswith("portal."):
            return {"ok": True, "text": json.dumps(FORWARD_RECORD)}
        return {"ok": True, "text": ""}

    @staticmethod
    def _not_found(operation, _args):
        return {"ok": True, "text": ""}

    def test_no_planted_identifier_survives_into_the_packet(self):
        """The one that matters. Everything else is how we got here."""
        packet, _events = self._run(self._found)
        blob = json.dumps(packet, ensure_ascii=False)
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_no_planted_identifier_survives_into_any_streamed_step(self):
        _packet, events = self._run(self._found)
        blob = json.dumps([e for e in events if e.get("type") == "subagent_step"],
                          ensure_ascii=False)
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_the_real_id_goes_out_on_the_wire_and_only_there(self):
        packet, _events = self._run(self._found)
        sent = [args.get("tracking_id") for op, args in self.calls if op.startswith("portal.")]
        # `auto` asks both channels, so both requests carry the real id — that is the point of the
        # split: the real value goes out, and nothing else ever sees it.
        self.assertEqual(sent, [TRACKING, TRACKING])
        self.assertNotIn(TRACKING, json.dumps(packet))           # the packet does not

    def test_a_hit_is_reduced_to_categories(self):
        packet, _events = self._run(self._found)
        item = [e for e in packet["evidence"] if e["kind"] == "portal_delivery"][0]
        self.assertTrue(item["record_found"])
        self.assertEqual(item["delivery_status"], "failed")
        self.assertEqual(item["failure_category"], "provider")   # "vendor gateway timeout"
        self.assertTrue(item["tracking_ref"].startswith("<tracking:"))
        self.assertNotIn("recipient", item)
        self.assertNotIn("messageContent", item)

    def test_not_found_is_a_result_not_a_delivery_confirmation(self):
        packet, _events = self._run(self._not_found)
        item = [e for e in packet["evidence"] if e["kind"] == "portal_delivery"][0]
        self.assertFalse(item["record_found"])
        self.assertIn("not evidence of successful delivery", item["note"])
        self.assertIn("not evidence of no business impact", item["note"])

    def test_auto_tries_sms_then_email_at_most_once_each(self):
        _packet, _events = self._run(self._not_found)
        portal_calls = [op for op, _ in self.calls if op.startswith("portal.")]
        self.assertEqual(portal_calls,
                         ["portal.sms_by_tracking_id", "portal.email_by_tracking_id"])

    def test_an_explicit_channel_calls_only_that_one(self):
        _packet, _events = self._run(self._not_found, portal_channel="email")
        portal_calls = [op for op, _ in self.calls if op.startswith("portal.")]
        self.assertEqual(portal_calls, ["portal.email_by_tracking_id"])

    def test_sms_failing_does_not_cost_the_email_lookup(self):
        def _responder(operation, _args):
            if operation == "portal.sms_by_tracking_id":
                raise mcp_client.TransportError("sms portal unreachable")
            return {"ok": True, "text": json.dumps(FORWARD_RECORD)}

        packet, _events = self._run(_responder)
        self.assertEqual([q["operation"] for q in packet["portal_queries"]["failed"]],
                         ["portal.sms_by_tracking_id"])
        self.assertEqual([q["operation"] for q in packet["portal_queries"]["executed"]],
                         ["portal.email_by_tracking_id"])
        self.assertTrue([e for e in packet["evidence"] if e["kind"] == "portal_delivery"])

    def test_a_tool_reported_error_never_becomes_a_delivery_verdict(self):
        def _responder(operation, _args):
            return {"ok": False, "tool_reported_error": True, "text": "unknown tracking id format"}

        packet, _events = self._run(_responder)
        self.assertEqual([e for e in packet["evidence"] if e["kind"] == "portal_delivery"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("REPORTED AN ERROR", joined)
        self.assertIn("never read it as", joined)

    def test_an_unrecognised_forward_shape_fails_closed(self):
        """Their §2.6: only the not-found path has been exercised live, so a shape we cannot read
        is OUR wiring gap and must never be reported as an empty record."""
        def _responder(operation, _args):
            return {"ok": True, "text": json.dumps({"someFutureField": 1, "another": "x"})}

        packet, _events = self._run(_responder)
        self.assertEqual([e for e in packet["evidence"] if e["kind"] == "portal_delivery"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("could not read the response shape", joined)
        self.assertIn("NOT an empty record", joined)

    def test_three_way_accounting_is_kept(self):
        packet, _events = self._run(self._not_found)
        ledger = packet["portal_queries"]
        self.assertEqual(len(ledger["attempted"]), 2)
        self.assertEqual(len(ledger["executed"]), 2)
        self.assertEqual(ledger["failed"], [])

    def test_mcp_off_makes_zero_portal_calls(self):
        with mock.patch.object(inv.config, "MCP_ENABLED", False):
            packet, _events = self._run(self._found)
        self.assertEqual([op for op, _ in self.calls if op.startswith("portal.")], [])
        self.assertTrue(any("switched off" in note for note in packet["not_investigated"]))

    def test_an_unwired_portal_operation_is_refused_locally(self):
        def _responder(operation, _args):
            raise mcp_registry.NotWired(f"{operation} has no tool mapping")

        packet, _events = self._run(_responder)
        self.assertEqual(len(packet["portal_queries"]["failed"]), 2)
        self.assertTrue(all(q.get("refused_locally") for q in packet["portal_queries"]["failed"]))


class BranchIndependenceTests(unittest.TestCase):
    """Their §2.5 and §9: no branch may block another."""

    def setUp(self):
        self.enabled = mock.patch.object(inv.config, "MCP_ENABLED", True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def test_a_portal_explosion_leaves_the_log_branch_alone(self):
        calls = []

        def _call(operation, args=None, **_kw):
            calls.append(operation)
            if operation.startswith("portal."):
                raise mcp_client.TransportError("portal down")
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            return {"ok": True, "text": ""}

        alert = ("prodECS_mc-hk-hase-x_service_CPU at 2026-07-30 03:15 HKT\n"
                 "trackingId: %s" % TRACKING)
        with mock.patch.object(mcp_client, "call", _call), \
             mock.patch.object(inv.incident, "parse_alert", lambda text, repos=None: {
                 "identified": True, "repos": [{"repo": "mc-hk-hase-x"}], "use_cases": [],
                 "times": [{"text": "2026-07-30 03:15 HKT", "timezone": "Asia/Hong_Kong",
                            "normalized": "2026-07-30 03:15:00"}],
                 "metric": "", "notes": [], "environment": "prod"}):
            packet = inv.investigate(alert)

        self.assertTrue([op for op in calls if op.startswith("portal.")])
        self.assertIn("log.list_apps", calls)          # the log branch still ran
        self.assertEqual(len(packet["portal_queries"]["failed"]), 2)


class CloudWatchContextTests(unittest.TestCase):
    """alarm_history / recent_changes: context about the alarm, never a stated cause."""

    def test_a_resource_is_only_taken_from_an_explicit_dimension(self):
        self.assertEqual(inv._explicit_resource(
            {"dimensions": [{"Name": "ServiceName", "Value": "csl-sms-deli"}]}), "csl-sms-deli")
        self.assertEqual(inv._explicit_resource(
            {"dimensions": [{"Name": "ClusterName", "Value": "prod-cluster"}]}), "prod-cluster")

    def test_a_dimension_that_does_not_name_a_resource_is_ignored(self):
        """Their §3.4: never assemble a resource from an alarm name or a repo id."""
        for name in ("AlarmName", "Region", "Environment", "Repo", "Namespace"):
            with self.subTest(name=name):
                self.assertEqual(
                    inv._explicit_resource({"dimensions": [{"Name": name, "Value": "something"}]}),
                    "")

    def test_no_dimensions_at_all_yields_no_resource(self):
        for identity in ({}, {"dimensions": []}, {"dimensions": None}, {"dimensions": "x"}):
            with self.subTest(identity=identity):
                self.assertEqual(inv._explicit_resource(identity), "")

    def test_relative_position_is_a_category_never_a_timestamp(self):
        window = {"start_utc": "2026-07-30 02:45:00", "end_utc": "2026-07-30 03:45:00",
                  "alert_utc": "2026-07-30 03:15:00"}
        self.assertEqual(inv._relative_to_alarm("2026-07-30 03:00:00", window), "before_alarm")
        self.assertEqual(inv._relative_to_alarm("2026-07-30 03:30:00", window), "after_alarm")
        self.assertEqual(inv._relative_to_alarm("2026-07-30 09:00:00", window), "outside_window")
        self.assertEqual(inv._relative_to_alarm("", window), "unknown")


if __name__ == "__main__":
    unittest.main()
