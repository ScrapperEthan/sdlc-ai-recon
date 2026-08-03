"""The CloudWatch Logs chain: resource -> log groups -> bounded Insights query -> evidence.

Why it matters: LogDream resolves an app for only a fraction of the estate (84/460 as of
2026-08-03), so for every other repo CloudWatch Logs is the ONLY log evidence that exists.

Acceptance list is the intranet handoff §8. The two structural properties under test are (a) the
sub-branches are independent — a metric failure must not stop the log chain, which is exactly what
the old single-function `_cloudwatch_branch` did — and (b) the query string is built here, from a
template, and can never be influenced by the model.
"""
import json
import unittest
from unittest import mock

from retriever import code as rcode, incident
from webapp import incident_investigator as inv, incident_parse, mcp_client

ALARM_WITH_RESOURCE = {
    "AlarmName": "prod-csl-sms-cpu", "Namespace": "AWS/ECS", "MetricName": "CPUUtilization",
    "Dimensions": [{"Name": "ServiceName", "Value": "csl-sms-deli"}],
    "Statistic": "Average", "Period": 300, "EvaluationPeriods": 1,
    "Threshold": 80, "ComparisonOperator": "GreaterThanThreshold",
}
# Same shape the existing CloudWatch tests use: a labelled AlarmName line plus a stamp that carries
# BOTH a date and a zone, which is what makes the plan runnable at all.
CW_ALERT = ("AlarmName: prod-csl-sms-cpu\n"
            "prodECS_service_CPUUtilizationMINOR[80percent]\n"
            "StateChangeTime: 2026-07-30 03:15 HKT")
LOG_GROUPS = {"logGroups": [{"logGroupName": "/ecs/csl-sms-deli"},
                            {"logGroupName": "/ecs/csl-sms-deli-sidecar"}]}
# Insights returns one row per hit, each row a list of {field, value} cells.
LOGS_HIT = {"results": [[{"field": "@timestamp", "value": "2026-07-30T03:15:01Z"},
                         {"field": "@message",
                          "value": "ERROR SocketTimeoutException for 9123 4567 "
                                   "customer alice.wong@example.com"}]]}
NO_LOGS_MESSAGE = "not evidence that the service writes no logs"


class CloudWatchLogsChainTests(unittest.TestCase):
    def setUp(self):
        self.enabled = mock.patch.object(inv.config, "MCP_ENABLED", True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)
        # `parse_alert` reads index/repo_tags.json, which is gitignored and absent here. Pinned so
        # these tests describe the CODE rather than whatever artefacts happen to be on disk — the
        # same reason the other investigator tests pin it.
        self._parse = mock.patch.object(incident, "parse_alert", lambda *a, **k: {
            "identified": True,
            "repos": [{"repo": "mc-hk-hase-x", "confidence": "confirmed"}],
            "use_cases": [], "metric": "CPUUtilization", "notes": [], "environment": "prod",
            "times": [{"text": "2026-07-30 03:15 HKT", "timezone": "Asia/Hong_Kong",
                       "ambiguous": False, "normalized": "2026-07-30 03:15:00"}]})
        self._parse.start()
        self.addCleanup(self._parse.stop)
        self._search = mock.patch.object(rcode, "search_code", lambda *a, **k: [])
        self._search.start()
        self.addCleanup(self._search.stop)
        # The arg maps the intranet has locked in. Pinned for the same reason: the committed
        # template is a placeholder, and these tests describe the code, not the template.
        self._opsmap = mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
            "aws.get_alarm": {"args": {"alarm_name": "alarmName"}},
            "aws.metric_window": {"args": {
                "namespace": "namespace", "metric": "metricName", "dimensions": "dimensions",
                "statistic": "statistic", "period_seconds": "periodSeconds",
                "from_time": "startTime", "to_time": "endTime"}},
            "aws.alarm_history": {"args": {"alarm_name": "alarmName", "from_time": "startTime",
                                            "to_time": "endTime", "max_results": "maxResults"}},
            "aws.recent_changes": {"args": {"alarm_name": "alarmName", "from_time": "startTime",
                                             "to_time": "endTime", "resource": "resourceName",
                                             "max_results": "maxResults"}},
            "aws.log_groups_for_resource": {"args": {
                "resource": "resourceName", "resource_arn": "resourceArn",
                "resource_type": "resourceType", "max_results": "maxResults"}},
            "aws.query_logs": {"args": {"log_groups": "logGroupNames", "query": "queryString",
                                         "from_time": "startTime", "to_time": "endTime",
                                         "limit": "limit"}}})
        self._opsmap.start()
        self.addCleanup(self._opsmap.stop)

    def _run(self, overrides=None, alarm=None):
        overrides = overrides or {}
        self.calls = []

        def _call(operation, args=None, **_kw):
            self.calls.append((operation, args or {}))
            if operation in overrides:
                behaviour = overrides[operation]
                if isinstance(behaviour, Exception):
                    raise behaviour
                return behaviour
            if operation == "aws.get_alarm":
                return {"ok": True, "text": json.dumps(alarm or ALARM_WITH_RESOURCE)}
            if operation == "aws.metric_window":
                return {"ok": True, "text": json.dumps({"Datapoints": []})}
            if operation == "aws.log_groups_for_resource":
                return {"ok": True, "text": json.dumps(LOG_GROUPS)}
            if operation == "aws.query_logs":
                return {"ok": True, "text": json.dumps(LOGS_HIT)}
            return {"ok": True, "text": ""}

        with mock.patch.object(mcp_client, "call", _call):
            events = list(inv.investigate_events(CW_ALERT))
        packet = [e for e in events if e.get("type") == "result"][0]["packet"]
        return packet, events

    def _ops(self):
        return [op for op, _ in self.calls]

    # --- independence ---------------------------------------------------------------------------

    def test_a_metric_failure_no_longer_stops_the_logs_chain(self):
        """The restructure this required: `_cloudwatch_branch` used to `return` on a metric
        failure, so Logs would never have run on exactly the incidents where the metric was
        missing — the ones where log evidence matters most."""
        packet, _events = self._run({"aws.metric_window": mcp_client.TransportError("metric down")})
        self.assertIn("aws.log_groups_for_resource", self._ops())
        self.assertIn("aws.query_logs", self._ops())
        self.assertTrue([e for e in packet["evidence"] if e["kind"] == "cloudwatch_logs"])

    def test_a_logs_failure_leaves_the_metric_evidence_alone(self):
        packet, _events = self._run({
            "aws.metric_window": {"ok": True, "text": json.dumps(
                {"Id": "m1", "Label": "CPUUtilization", "StatusCode": "Complete",
                 "Timestamps": ["2026-07-30T03:05:00Z", "2026-07-30T03:15:00Z"],
                 "Values": [12.0, 91.0]})},
            "aws.log_groups_for_resource": mcp_client.TransportError("logs down")})
        self.assertTrue([e for e in packet["evidence"] if e["kind"] == "cloudwatch_metric"])
        self.assertEqual(len(packet["cloudwatch_logs"]["failed"]), 1)

    # --- the resource must be explicit ------------------------------------------------------------

    def test_no_explicit_resource_means_zero_logs_calls(self):
        bare = dict(ALARM_WITH_RESOURCE, Dimensions=[{"Name": "Region", "Value": "ap-east-1"}])
        packet, _events = self._run(alarm=bare)
        self.assertNotIn("aws.log_groups_for_resource", self._ops())
        self.assertNotIn("aws.query_logs", self._ops())
        joined = " ".join(packet["not_investigated"])
        self.assertIn("no explicit resource name or ARN", joined)
        self.assertIn("not 'the service has no logs'", joined)

    def test_an_arn_dimension_goes_to_resource_arn_not_resource_name(self):
        """Confirmed by the intranet: `resource`->resourceName, `resource_arn`->resourceArn. They
        are NOT interchangeable; sending a name where an ARN is required queries nothing."""
        arn = "arn:aws:ecs:ap-east-1:1234:service/csl"
        got = inv._explicit_resource_identity({"dimensions": [{"Name": "ServiceName",
                                                               "Value": arn}]})
        self.assertEqual(got["resource_arn"], arn)
        self.assertEqual(got["resource"], "")

    def test_resource_type_is_left_empty_rather_than_invented(self):
        got = inv._explicit_resource_identity(
            {"dimensions": [{"Name": "ServiceName", "Value": "csl-sms-deli"}]})
        self.assertEqual(got["resource_type"], "")
        self.assertEqual(got["resource"], "csl-sms-deli")
        self.assertEqual(got["dimension_name"], "ServiceName")

    # --- the query string is ours ------------------------------------------------------------------

    def test_the_query_string_is_built_from_a_fixed_template(self):
        query = inv._fixed_logs_query("SocketTimeout")
        self.assertTrue(query.startswith("fields @timestamp, @message"))
        self.assertIn("| filter @message like /SocketTimeout/", query)
        self.assertIn("| sort @timestamp desc", query)
        self.assertIn("| limit %d" % inv._MAX_LOGS_LIMIT, query)

    def test_regex_and_pipe_metacharacters_never_reach_the_query(self):
        """A keyword is a literal WE derived, not a language the model may supply."""
        for hostile in ("a/ | delete", ".*", "x/ | fields @log | limit 9999", "a\nb", "//"):
            with self.subTest(hostile=hostile):
                query = inv._fixed_logs_query(inv._bounded_keyword(hostile))
                self.assertEqual(query.count("|"), 3)      # filter, sort, limit — and no more
                self.assertNotIn("/", inv._bounded_keyword(hostile))

    def test_keyword_count_and_length_are_capped(self):
        plan = {"keywords": [{"term": "kw%d" % i + "x" * 200} for i in range(20)]}
        keywords = inv._bounded_logs_keywords(plan)
        self.assertLessEqual(len(keywords), inv._MAX_LOGS_KEYWORDS)
        for keyword in keywords:
            self.assertLessEqual(len(keyword), inv._MAX_LOGS_KEYWORD_CHARS)

    def test_log_groups_are_capped(self):
        many = {"logGroups": [{"logGroupName": "/ecs/g%d" % i} for i in range(30)]}
        self._run({"aws.log_groups_for_resource": {"ok": True, "text": json.dumps(many)}})
        sent = [args.get("log_groups") for op, args in self.calls if op == "aws.query_logs"]
        self.assertTrue(sent)
        for groups in sent:
            self.assertLessEqual(len(groups), inv._MAX_LOG_GROUPS)

    # --- the five outcomes stay distinct ----------------------------------------------------------

    def test_a_hit_keeps_the_exception_class_and_a_bounded_excerpt(self):
        packet, _events = self._run()
        item = [e for e in packet["evidence"] if e["kind"] == "cloudwatch_logs"][0]
        self.assertIn("SocketTimeoutException", item["exception_classes"])
        self.assertLessEqual(len(item["excerpts"]), inv._MAX_EXCERPTS)

    def test_no_planted_pii_survives_from_a_cloudwatch_log_line(self):
        """Same exit gate as LogDream: redact first, then bound."""
        packet, events = self._run()
        blob = json.dumps(packet, ensure_ascii=False) + json.dumps(
            [e for e in events if e.get("type") == "subagent_step"], ensure_ascii=False)
        self.assertNotIn("9123 4567", blob)
        self.assertNotIn("alice.wong@example.com", blob)

    def test_an_unreadable_logs_shape_is_a_wiring_gap_not_an_empty_log(self):
        packet, _events = self._run({"aws.query_logs": {"ok": True,
                                                        "text": json.dumps({"weird": {"a": 1}})}})
        self.assertEqual([e for e in packet["evidence"] if e["kind"] == "cloudwatch_logs"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("could not read the response shape", joined)
        self.assertIn("NOT an empty log", joined)

    def test_a_tool_error_never_becomes_log_evidence(self):
        packet, _events = self._run({"aws.query_logs": {"ok": False, "tool_reported_error": True,
                                                        "text": "invalid query syntax"}})
        self.assertEqual([e for e in packet["evidence"] if e["kind"] == "cloudwatch_logs"], [])
        self.assertIn("REPORTED AN ERROR", " ".join(packet["not_investigated"]))
        self.assertNotIn("invalid query syntax", json.dumps(packet["evidence"]))

    def test_a_successful_empty_result_is_not_a_failure(self):
        packet, _events = self._run({"aws.query_logs": {"ok": True, "text": ""}})
        self.assertEqual([e for e in packet["evidence"] if e["kind"] == "cloudwatch_logs"], [])
        self.assertEqual(packet["cloudwatch_logs"]["failed"], [])
        self.assertTrue(packet["cloudwatch_logs"]["executed"])

    def test_no_log_group_found_is_about_the_mapping_not_the_service(self):
        packet, _events = self._run({"aws.log_groups_for_resource":
                                     {"ok": True, "text": json.dumps({"logGroups": []})}})
        self.assertNotIn("aws.query_logs", self._ops())
        self.assertIn(NO_LOGS_MESSAGE, " ".join(packet["not_investigated"]))

    # --- accounting and gates ----------------------------------------------------------------------

    def test_the_logs_ledger_is_separate_from_the_metric_one(self):
        """Their §1.4: sharing a ledger makes 'which log chain failed' unanswerable."""
        packet, _events = self._run()
        logs_ops = {q["operation"] for bucket in packet["cloudwatch_logs"].values() for q in bucket}
        metric_ops = {q["operation"] for bucket in packet["cloudwatch_queries"].values()
                      for q in bucket}
        self.assertTrue(logs_ops <= {"aws.log_groups_for_resource", "aws.query_logs"})
        self.assertFalse(logs_ops & metric_ops)

    def test_mcp_off_makes_zero_logs_calls(self):
        with mock.patch.object(inv.config, "MCP_ENABLED", False):
            self._run()
        self.assertEqual(self._ops(), [])

    def test_the_operations_we_deliberately_left_unwired_are_never_called(self):
        """Three operations stay dark, each for a reason the intranet established LIVE. Silence
        here is the design, not an omission, and this test is what keeps it deliberate — without it
        the next person reads "3 of 15 unused" as a to-do list.

        `aws.parse_alert` — permanently. Probed 2026-08-03 with three synthetic alerts: a top-level
        JSON `AlarmName` was not recognised at all, and a multi-line alert had the following lines
        swallowed into the name (120 chars, embedded newline). Our local extractor is strictly
        better, needs no connection, and cannot let a remote parse override what the user said.

        `log.browse` — it takes only `source` + `path`, so wiring it would bypass the chain that
        makes log reads safe (app confirmed on that source -> real filename -> bounded read).
        `log.search_files` already provides what the product needs, with a verified shape.

        `log.investigate` — its INPUT schema is confirmed, its OUTPUT shape is not. The intranet
        could not probe it: LogDream's port refuses TCP from the deploy server. Its documented
        `findings / cause_chain / next_steps` are documented, not live-verified, and a tool's
        "next steps" must never enter evidence as fact.
        """
        self._run()
        for operation in ("aws.parse_alert", "log.browse", "log.investigate"):
            self.assertNotIn(operation, self._ops())

    def test_nothing_anywhere_in_the_engine_names_those_tools(self):
        """A caller could also appear by someone hardcoding the REAL tool name and bypassing the
        registry. These are the real names from their `tools/list`."""
        import pathlib
        root = pathlib.Path(inv.__file__).resolve().parent.parent
        sources = list((root / "webapp").glob("*.py")) + list((root / "retriever").glob("*.py"))
        for real_name in ("parse_cloudwatch_alert", "browse_logdream", "investigate_logdream"):
            # Quoted, i.e. usable as a value. Prose about WHY a tool is unwired is the point of
            # these comments, so only a string literal counts as naming it.
            for quoted in ('"%s"' % real_name, "'%s'" % real_name):
                for source in sources:
                    with self.subTest(tool=quoted, file=source.name):
                        self.assertNotIn(quoted, source.read_text(encoding="utf-8"))


class LogGroupParsingTests(unittest.TestCase):
    def test_a_list_of_names_or_of_objects_both_parse(self):
        self.assertEqual(incident_parse._parse_log_groups(["/ecs/a", "/ecs/b"]), ["/ecs/a", "/ecs/b"])
        self.assertEqual(incident_parse._parse_log_groups({"logGroups": [{"logGroupName": "/ecs/a"}]}),
                         ["/ecs/a"])

    def test_an_unknown_shape_is_none_not_empty(self):
        """None means parser gap; [] would mean 'this resource has no log groups'."""
        self.assertIsNone(incident_parse._parse_log_groups({"totallyUnexpected": {"a": 1}}))
        self.assertIsNone(incident_parse._parse_log_groups(None))


class LogLineParsingTests(unittest.TestCase):
    def test_only_explicit_message_fields_are_read(self):
        rows = {"results": [[{"field": "@timestamp", "value": "t"},
                             {"field": "@message", "value": "the line"}]]}
        self.assertEqual(incident_parse._parse_cloudwatch_log_lines(rows), ["the line"])

    def test_a_plain_object_list_with_message_keys_parses(self):
        self.assertEqual(incident_parse._parse_cloudwatch_log_lines([{"message": "one"}, {"@message": "two"}]),
                         ["one", "two"])

    def test_an_unknown_shape_is_none_never_stringified(self):
        """The 2026-07-30 defect class: str(body).splitlines() turns an error envelope into
        'log lines'. There must be no path that does that."""
        self.assertIsNone(incident_parse._parse_cloudwatch_log_lines({"error": "denied", "code": 403}))
        self.assertIsNone(incident_parse._parse_cloudwatch_log_lines(None))


if __name__ == "__main__":
    unittest.main()
