"""`aws.resource_tags`: ownership labels, but only from an ARN the alarm itself carried.

Their §2 gives the whole shape of this. The response format is confirmed live
(`{resourceArn, tags{}, rawTags[{Key,Value}]}`), so the remaining risk was never parsing — it is
SCOPE. A tag lookup needs an ARN; `get_alarm`'s dimensions almost always give a resource NAME, and
their scan of 500 real alarms found ZERO explicit ARNs. So on today's data this branch makes no call
at all, and that is the correct outcome, not a gap.

The reason it exists anyway is that the RULE is what matters: an ARN appearing in the alarm's own
dimension is by construction the alarm's target. An ARN assembled from a name, or the alarm's own
`AlarmArn`, would return real tags for the WRONG resource — worse than none, because it looks like
an answer. Test list is their §2.4.
"""
import json
import unittest
from unittest import mock

from webapp import incident_investigator as inv, mcp_client

SERVICE_ARN = "arn:aws:ecs:ap-east-1:111122223333:service/prod/csl-sms-deli"
ALARM_ARN = "arn:aws:cloudwatch:ap-east-1:111122223333:alarm:prod-csl-sms-cpu"
# Their confirmed live shape. `owner` is a PERSON here on purpose: the test asserts it never lands
# in the packet.
TAGS_BODY = {"resourceArn": SERVICE_ARN,
             "tags": {"owner": "alice.wong@example.com", "environment": "prod",
                      "cost-centre": "HK-1234"},
             "rawTags": [{"Key": "owner", "Value": "alice.wong@example.com"},
                         {"Key": "environment", "Value": "prod"},
                         {"Key": "cost-centre", "Value": "HK-1234"}]}
CW_ALERT = ("AlarmName: prod-csl-sms-cpu\n"
            "prodECS_service_CPUUtilizationMINOR[80percent]\n"
            "StateChangeTime: 2026-07-30 03:15 HKT")


def _alarm(dimension_value, dimension_name="ServiceName"):
    return {"AlarmName": "prod-csl-sms-cpu", "AlarmArn": ALARM_ARN, "Namespace": "AWS/ECS",
            "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": dimension_name, "Value": dimension_value}],
            "Statistic": "Average", "Period": 300, "EvaluationPeriods": 1,
            "Threshold": 80, "ComparisonOperator": "GreaterThanThreshold"}


class ResourceTagsTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.object(inv.config, "MCP_ENABLED", True),
            mock.patch.object(inv.incident, "parse_alert", lambda *a, **k: {
                "identified": True, "repos": [{"repo": "mc-hk-hase-x", "confidence": "confirmed"}],
                "use_cases": [], "metric": "CPUUtilization", "notes": [], "environment": "prod",
                "times": [{"text": "2026-07-30 03:15 HKT", "timezone": "Asia/Hong_Kong",
                           "ambiguous": False, "normalized": "2026-07-30 03:15:00"}]}),
            mock.patch.object(inv.rcode, "search_code", lambda *a, **k: []),
            mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "aws.get_alarm": {"args": {"alarm_name": "alarmName"}},
                "aws.metric_window": {"args": {"namespace": "namespace", "metric": "metricName",
                                                "from_time": "startTime", "to_time": "endTime"}},
                "aws.alarm_history": {"args": {"alarm_name": "alarmName"}},
                "aws.recent_changes": {"args": {"alarm_name": "alarmName"}},
                "aws.log_groups_for_resource": {"args": {"resource": "resourceName",
                                                          "resource_arn": "resourceArn"}},
                "aws.query_logs": {"args": {"log_groups": "logGroupNames", "query": "queryString"}},
                # The abstract arg is `resource`; their required parameter is `resourceArn`.
                "aws.resource_tags": {"args": {"resource": "resourceArn"}}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, alarm, tags=TAGS_BODY):
        self.calls = []

        def _call(operation, args=None, **_kw):
            self.calls.append((operation, args or {}))
            if operation == "aws.get_alarm":
                return {"ok": True, "text": json.dumps(alarm)}
            if operation == "aws.resource_tags":
                if isinstance(tags, Exception):
                    raise tags
                return tags if isinstance(tags, dict) and "ok" in tags else {
                    "ok": True, "text": json.dumps(tags)}
            return {"ok": True, "text": ""}

        with mock.patch.object(mcp_client, "call", _call):
            events = list(inv.investigate_events(CW_ALERT))
        packet = [e for e in events if e.get("type") == "result"][0]["packet"]
        return packet, events

    def _ops(self):
        return [op for op, _ in self.calls]

    def _tag_calls(self):
        return [args for op, args in self.calls if op == "aws.resource_tags"]

    # --- their §2.4, in order ---------------------------------------------------------------------

    def test_1_a_resource_name_means_zero_tag_calls(self):
        packet, _events = self._run(_alarm("csl-sms-deli"))
        self.assertEqual(self._tag_calls(), [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("resource NAME, not an ARN", joined)
        self.assertIn("says nothing about whether", joined)

    def test_2_an_explicit_arn_is_called_at_most_once(self):
        self._run(_alarm(SERVICE_ARN))
        self.assertEqual(len(self._tag_calls()), 1)

    def test_3_a_resource_name_is_never_sent_as_an_arn(self):
        self._run(_alarm("csl-sms-deli"))
        for args in self._tag_calls():
            self.assertNotIn("csl-sms-deli", json.dumps(args))

    def test_4_the_alarms_own_arn_is_never_used_as_the_resource_arn(self):
        """AlarmArn would return the ALARM's tags and present them as the service's."""
        self._run(_alarm("csl-sms-deli"))
        self.assertEqual(self._tag_calls(), [])
        self._run(_alarm(SERVICE_ARN))
        sent = json.dumps(self._tag_calls())
        self.assertIn(SERVICE_ARN, sent)
        self.assertNotIn(ALARM_ARN, sent)

    def test_5a_a_transport_error_produces_no_evidence(self):
        packet, _events = self._run(_alarm(SERVICE_ARN),
                                    tags=mcp_client.TransportError("tags down"))
        self.assertEqual([e for e in packet["evidence"]
                          if e["kind"] == "cloudwatch_resource_tags"], [])
        self.assertEqual(len(packet["cloudwatch_tags"]["failed"]), 1)

    def test_5b_a_tool_error_produces_no_evidence(self):
        packet, _events = self._run(_alarm(SERVICE_ARN),
                                    tags={"ok": False, "tool_reported_error": True,
                                          "text": "AccessDenied"})
        self.assertEqual([e for e in packet["evidence"]
                          if e["kind"] == "cloudwatch_resource_tags"], [])
        self.assertIn("not an absence of tags", " ".join(packet["not_investigated"]))

    def test_5c_an_unreadable_shape_is_a_wiring_gap_not_an_untagged_resource(self):
        packet, _events = self._run(_alarm(SERVICE_ARN), tags={"somethingElse": [1, 2]})
        self.assertEqual([e for e in packet["evidence"]
                          if e["kind"] == "cloudwatch_resource_tags"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("could not read the response shape", joined)
        self.assertIn("NOT an untagged resource", joined)

    def test_6_no_arn_tag_value_or_person_name_reaches_the_packet_or_the_stream(self):
        """The load-bearing one. A tag value is where the person's name lives."""
        packet, events = self._run(_alarm(SERVICE_ARN))
        blob = json.dumps(packet, ensure_ascii=False) + json.dumps(
            [e for e in events if e.get("type") == "subagent_step"], ensure_ascii=False)
        for secret in (SERVICE_ARN, ALARM_ARN, "alice.wong@example.com", "HK-1234",
                       "111122223333"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_7_a_tags_failure_does_not_disturb_the_other_sub_branches(self):
        packet, _events = self._run(_alarm(SERVICE_ARN),
                                    tags=mcp_client.TransportError("tags down"))
        self.assertIn("aws.metric_window", self._ops())
        self.assertIn("aws.alarm_history", self._ops())
        self.assertIn("aws.log_groups_for_resource", self._ops())

    # --- what the evidence is allowed to say -------------------------------------------------------

    def test_only_key_presence_is_recorded_never_values(self):
        packet, _events = self._run(_alarm(SERVICE_ARN))
        item = [e for e in packet["evidence"] if e["kind"] == "cloudwatch_resource_tags"][0]
        self.assertEqual(item["tag_count"], 3)
        self.assertTrue(item["owner_tag_present"])
        self.assertTrue(item["environment_tag_present"])
        self.assertFalse(item["application_tag_present"])
        self.assertFalse(item["support_group_tag_present"])
        self.assertNotIn("tags", item)
        self.assertNotIn("resourceArn", item)

    def test_the_note_forbids_turning_an_owner_tag_into_a_repo_mapping(self):
        """Their §2.3: `owner` alone does not establish which repository this is."""
        packet, _events = self._run(_alarm(SERVICE_ARN))
        item = [e for e in packet["evidence"] if e["kind"] == "cloudwatch_resource_tags"][0]
        self.assertIn("does NOT establish which repository", item["note"])

    def test_rawtags_is_the_fallback_when_the_tags_object_is_absent(self):
        body = {"resourceArn": SERVICE_ARN,
                "rawTags": [{"Key": "owner", "Value": "someone"}, {"Key": "team", "Value": "x"}]}
        self.assertEqual(sorted(inv._tag_keys(body)), ["owner", "team"])

    def test_the_tags_object_is_preferred_over_rawtags(self):
        body = {"tags": {"a": "1"}, "rawTags": [{"Key": "b", "Value": "2"}]}
        self.assertEqual(inv._tag_keys(body), ["a"])

    def test_an_unknown_shape_is_none_not_an_empty_tag_list(self):
        self.assertIsNone(inv._tag_keys({"nothing": "useful"}))
        self.assertIsNone(inv._tag_keys(["not", "an", "object"]))

    def test_mcp_off_makes_zero_tag_calls(self):
        with mock.patch.object(inv.config, "MCP_ENABLED", False):
            self._run(_alarm(SERVICE_ARN))
        self.assertEqual(self._ops(), [])


class EndpointNeverLeaksTests(unittest.TestCase):
    """Addresses are kept out of git — but the exception TEXT is what lands in `not_investigated`,
    which is persisted to chat_sessions.json and rendered in the browser. Keeping the URL out of
    the repo while writing it into stored chat history would be a hole in the same rule."""

    def test_a_transport_error_names_the_server_not_the_address(self):
        for reason, expected_kind in (
                ("[Errno 111] Connection refused", "connection_refused"),
                ("[Errno -2] Name or service not known", "dns_failure"),
                ("timed out", "timeout")):
            with self.subTest(reason=reason):
                self.assertEqual(mcp_client._reason_kind(reason), expected_kind)

    def test_an_upstream_error_body_is_stripped_of_urls(self):
        body = "proxy denied for http://logdream.internal.example:8092/sse see docs"
        cleaned = mcp_client._safe_detail(body)
        self.assertNotIn("logdream.internal.example", cleaned)
        self.assertIn("<endpoint>", cleaned)

    def test_the_error_body_is_bounded(self):
        self.assertLessEqual(len(mcp_client._safe_detail("x" * 5000)), 200)

    def test_the_investigator_explains_a_refusal_as_a_service_problem(self):
        """Their whole 8092 diagnosis in one sentence: refused points at the listener, not the
        network — and it is never 'there were no logs'."""
        note = inv._transport_note(
            mcp_client.TransportError("logdream MCP unreachable: connection_refused",
                                      kind="connection_refused"))
        self.assertIn("REFUSED", note)
        self.assertIn("not at the network", note)
        self.assertIn("Nothing was read", note)

    def test_a_timeout_is_explained_differently_from_a_refusal(self):
        note = inv._transport_note(
            mcp_client.TransportError("x", kind="timeout"))
        self.assertIn("never as 'no results'", note)


if __name__ == "__main__":
    unittest.main()
