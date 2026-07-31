"""The incident investigator — the wall between production logs and persisted chat history.

The load-bearing tests are the leak tests: plant PII in a log response, run the investigator, and
assert it cannot be found anywhere in what comes back. The packet is exactly what gets persisted to
chat_sessions.json, so "not in the packet" is the property that matters, not "we redacted carefully".
"""
import json
import unittest
from unittest import mock

from webapp import config, incident_investigator as inv, mcp_client, mcp_registry


# A full stamp, not a bare `03:15 HKT`: the real read tool backtracks from an `alert_time` and
# takes the zone separately (intranet, 2026-07-31), so a clock time with no DATE is not a runnable
# alert. The old fixture said 03:15 and passed, which is precisely how that gap survived.
ALERT = ("prodECS_mc-hk-hase-csl-sms-deli-job_service_CPUUtilizationMINOR[80percent] "
         "at 2026-07-30 03:15 HKT")
ALERT_TIMES = [{"text": "2026-07-30 03:15 HKT", "timezone": "Asia/Hong_Kong",
                "ambiguous": False, "normalized": "2026-07-30 03:15:00"}]
DIRTY_LOG = "\n".join([
    "2026-07-30 03:15:01 ERROR SmsDeliveryException customer alice.wong@example.com failed",
    "2026-07-30 03:15:02 WARN  retry for 9123 4567 ref MDCTRACK-9F2K-88H1",
    "2026-07-30 03:15:03 ERROR TimeoutException acct 4123456789012345 gave up",
    "2026-07-30 03:15:04 ERROR SmsDeliveryException hkid A1234567 second attempt",
    "2026-07-30 03:15:05 INFO  recovered",
    "2026-07-30 03:15:06 INFO  this sixth line is past the excerpt cap",
])
SECRETS = ("alice.wong@example.com", "9123 4567", "4123456789012345", "A1234567",
           "MDCTRACK-9F2K-88H1")


class RedactionTests(unittest.TestCase):
    def test_every_pii_shape_is_replaced_by_a_marker(self):
        counts = {}
        out = inv.redact(DIRTY_LOG, counts)
        for secret in SECRETS:
            self.assertNotIn(secret, out, secret)
        self.assertTrue(counts)

    def test_the_same_value_gets_the_same_marker_so_correlation_survives(self):
        """A plain *** would destroy the one thing that makes an excerpt worth reading: that these
        forty lines concern ONE message."""
        one = inv.redact("ref MDCTRACK-9F2K-88H1 failed")
        two = inv.redact("ref MDCTRACK-9F2K-88H1 retried")
        marker = one.split("ref ")[1].split(" ")[0]
        self.assertIn(marker, two)
        self.assertRegex(marker, r"^<tracking:[0-9a-f]{6}>$")

    def test_redaction_is_idempotent(self):
        once = inv.redact(DIRTY_LOG)
        self.assertEqual(inv.redact(once), once)

    def test_what_makes_a_log_useful_survives_redaction(self):
        out = inv.redact(DIRTY_LOG)
        self.assertIn("SmsDeliveryException", out)
        self.assertIn("TimeoutException", out)
        self.assertIn("2026-07-30 03:15:01", out)

    def test_the_exit_gate_strips_what_upstream_redaction_missed(self):
        """Defence 2. Reaching it means a bug, so it must be counted, not fixed quietly.

        It MASKS the matching span rather than discarding the whole string: most strings that reach
        it are prose we composed, where blanking the sentence costs an operator the reason and
        protects nothing extra."""
        packet, report = inv.sanitize_packet(
            {"evidence": [{"excerpts": ["leaked bob@example.com here"], "lines_seen": 1}]})
        self.assertEqual(report["sanitized_at_exit"], 1)
        self.assertEqual(report["kinds"], ["email"])
        self.assertNotIn("bob@example.com", json.dumps(packet))
        masked = packet["evidence"][0]["excerpts"][0]
        self.assertTrue(masked.startswith("leaked ") and masked.endswith(" here"), masked)

    def test_a_dated_log_filename_is_not_mistaken_for_a_phone_number(self):
        """`otx_trace.log.20260701` is eight consecutive digits. Before the identifier exemption the
        gate blanked whole operator messages containing one — and inflated `sanitized_at_exit`, the
        counter whose only job is to flag a REAL leak."""
        packet, report = inv.sanitize_packet(
            {"file": "otx_trace.log.20260701", "app": "cslSmsDeli", "source": "hkl"})
        self.assertEqual(packet["file"], "otx_trace.log.20260701")
        self.assertEqual(report["sanitized_at_exit"], 0)

    def test_the_exit_gate_leaves_clean_text_alone(self):
        packet, report = inv.sanitize_packet({"note": "SmsDeliveryException x12 on hk1"})
        self.assertEqual(report["sanitized_at_exit"], 0)
        self.assertEqual(packet["note"], "SmsDeliveryException x12 on hk1")


class AppNameTests(unittest.TestCase):
    def test_a_rule_derived_name_is_a_candidate_never_an_answer(self):
        """RUNBOOK-55 measured repo->app at 0% identical and ~36% by rule."""
        got = inv.app_candidates("mc-hk-hase-csl-sms-deli-job")
        self.assertTrue(got)
        self.assertEqual({c["confidence"] for c in got}, {"candidate"})
        self.assertEqual(got[0]["app"], "cslSmsDeli")

    def test_an_intranet_mapping_wins_and_is_confirmed(self):
        with mock.patch.object(inv, "_app_map", lambda: {"mc-hk-hase-x-job": "theRealApp"}):
            got = inv.app_candidates("mc-hk-hase-x-job")
        self.assertEqual(got, [{"app": "theRealApp",
                                 "how": "config/logdream_apps.json (intranet-owned mapping)",
                                 "confidence": "confirmed"}])

    def test_a_missing_mapping_file_is_normal_not_an_error(self):
        self.assertEqual(inv._app_map(), {})

    def test_either_filename_and_either_shape_is_accepted(self):
        """config/ is intranet-owned on a box that cannot push, and their gap analysis names this
        file differently than this module first did. A name disagreement would show up as the knob
        silently doing nothing — the worst failure mode for a knob."""
        import os as _os
        import tempfile
        for name in inv._APP_MAP_FILES:
            for payload in ({"repo_to_app": {"repo-a": "appA"}},   # documented shape
                            {"repo-a": "appA", "_note": "flat"}):  # hand-written flat shape
                with tempfile.TemporaryDirectory() as tmp:
                    _os.makedirs(_os.path.join(tmp, "config"))
                    with open(_os.path.join(tmp, "config", name), "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                    with mock.patch.object(_os, "getcwd", lambda tmp=tmp: tmp):
                        self.assertEqual(inv._app_map(), {"repo-a": "appA"},
                                         f"{name} / {sorted(payload)}")


class PlanTests(unittest.TestCase):
    """The plan opens no sockets, so it is fully testable offline."""

    def _plan(self, text, **kw):
        with mock.patch.object(inv.incident, "parse_alert", self._parsed(text)):
            return inv.plan(text, **kw)

    @staticmethod
    def _parsed(_text, identified=True, times=None, repos=None):
        def _fake(*_a, **_k):
            return {"identified": identified,
                    "repos": repos if repos is not None else [
                        {"repo": "mc-hk-hase-csl-sms-deli-job", "confidence": "confirmed"}],
                    "use_cases": [], "times": times or [], "metric": "CPUUtilization",
                    "notes": [], "environment": "prod"}
        return _fake

    def test_an_unidentifiable_alert_plans_nothing_and_says_why(self):
        with mock.patch.object(inv.incident, "parse_alert",
                               self._parsed("", identified=False, repos=[])):
            out = inv.plan("something broke")
        self.assertFalse(out["ok"])
        self.assertTrue(out["refusals"])
        self.assertIn("do not guess an app", out["refusals"][0])

    def test_a_time_without_a_timezone_builds_no_window(self):
        """Three timezones coexist; a guessed window returns nothing and reads as 'no anomaly'."""
        out = self._plan(ALERT, **{})
        self.assertIsNone(out["window"])
        self.assertTrue(any("a TIMEZONE" in r for r in out["refusals"]))

    def test_an_explicit_zone_in_the_alert_is_used(self):
        with mock.patch.object(inv.incident, "parse_alert", self._parsed(ALERT, times=ALERT_TIMES)):
            out = inv.plan(ALERT)
        self.assertEqual(out["window"]["timezone"], "Asia/Hong_Kong")
        self.assertEqual(out["window"]["source"], "explicit in the alert text")
        self.assertEqual(out["window"]["alert_time"], "2026-07-30 03:15:00")

    def test_the_alerts_own_zone_beats_a_caller_supplied_one(self):
        with mock.patch.object(inv.incident, "parse_alert", self._parsed(
                ALERT, times=[{"text": "2026-07-30 03:15 Z", "timezone": "UTC",
                               "normalized": "2026-07-30 03:15:00"}])):
            out = inv.plan(ALERT, timezone="Asia/Hong_Kong")
        self.assertEqual(out["window"]["timezone"], "UTC")

    def test_a_caller_supplied_zone_rescues_an_ambiguous_time(self):
        with mock.patch.object(inv.incident, "parse_alert", self._parsed(
                ALERT, times=[{"text": "2026-07-30 03:15", "timezone": "", "ambiguous": True,
                               "normalized": "2026-07-30 03:15:00"}])):
            out = inv.plan(ALERT, timezone="Asia/Hong_Kong")
        self.assertEqual(out["window"]["timezone"], "Asia/Hong_Kong")
        self.assertIn("caller-supplied", out["window"]["source"])

    def test_keywords_carry_the_reason_they_are_in_the_list(self):
        """The keywords are the part a generic AIOps cannot produce, so each must be attributable."""
        out = self._plan(ALERT)
        self.assertTrue(out["keywords"])
        for item in out["keywords"]:
            self.assertTrue(item["why"])
        self.assertIn("CPUUtilization", [k["term"] for k in out["keywords"]])

    def test_exception_classes_come_from_the_repo_source_not_from_a_guess(self):
        hits = ["a/B.java:10:    throw new SmsDeliveryException(msg);",
                "a/C.java:20:    throw new SmsDeliveryException(other);",
                "a/D.java:30:    throw new VendorTimeoutError(x);"]
        with mock.patch.object(inv.rcode, "search_code", lambda *a, **k: hits):
            got = inv.exception_classes("mc-hk-hase-csl-sms-deli-job")
        self.assertEqual(got, ["SmsDeliveryException", "VendorTimeoutError"])

    def test_an_unavailable_mirror_offers_no_keywords_rather_than_invented_ones(self):
        def _boom(*_a, **_k):
            raise OSError("mirror not present")
        with mock.patch.object(inv.rcode, "search_code", _boom):
            self.assertEqual(inv.exception_classes("any-repo"), [])


class InvestigateTests(unittest.TestCase):
    """The end-to-end wall test: raw production text in, sanitized packet out."""

    def setUp(self):
        # Retention pinned OFF: everything in this file describes the DEFAULT behaviour, and the
        # devops box has SDLC_INCIDENT_RAW_LOGS=1 set for the internal test. Without pinning, these
        # would be graded against whatever the deployment happens to have — the same trap that made
        # ShippedConfigTests read the box's local MCP config instead of the committed one.
        # Retention-on behaviour has its own file, tests/test_incident_raw_store.py.
        self._flag = mock.patch.object(config, "MCP_ENABLED", True)
        self._flag.start()
        self._raw = mock.patch.object(config, "INCIDENT_RAW_LOGS", False)
        self._raw.start()
        # Source names and the log.list_apps arg map are read from config, which differs between the
        # committed template and the box's local file. Pinned here for the same reason retention is:
        # these tests describe the CODE, and must not be graded against whatever config is on disk.
        self._sources = mock.patch.object(inv, "log_sources", lambda: inv.DEFAULT_LOG_SOURCES)
        self._sources.start()
        # Mirrors what the intranet will fill in: every abstract arg mapped to a real parameter name.
        self._ops = mock.patch.object(
            inv.mcp_registry, "operations",
            lambda cfg=None: {
                "log.list_apps": {"args": {"source": "source"}},
                "log.search_files": {"args": {"app": "app", "source": "source",
                                               "keyword": "keyword", "date_hint": "date_hint"}},
                "log.read": {"args": {"app": "app", "source": "source", "file": "file_name",
                                       "mode": "read_mode", "keyword": "keyword",
                                       "alert_time": "alert_time", "timezone": "timezone",
                                       "max_lines": "lines",
                                       "backtrack_lines": "backtrack_lines"}}})
        self._ops.start()
        self.calls = []

        def _fake_call(operation, args=None, **_kw):
            self.calls.append((operation, dict(args or {})))
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli", "otherApp"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": json.dumps(
                    ["/apps/cslSmsDeli/log/otx_trace.log",
                     "/apps/cslSmsDeli/log/exception.log",
                     "/apps/cslSmsDeli/log/otx_trace.log.20260701"])}
            return {"ok": True, "text": DIRTY_LOG}

        self._mcp = mock.patch.object(mcp_client, "call", _fake_call)
        self._mcp.start()
        self._parse = mock.patch.object(inv.incident, "parse_alert", lambda *a, **k: {
            "identified": True,
            "repos": [{"repo": "mc-hk-hase-csl-sms-deli-job", "confidence": "confirmed"}],
            "use_cases": [], "metric": "CPUUtilization", "notes": [], "environment": "prod",
            "times": [dict(t) for t in ALERT_TIMES]})
        self._parse.start()
        self._search = mock.patch.object(inv.rcode, "search_code", lambda *a, **k: [])
        self._search.start()

    def tearDown(self):
        for patcher in (self._search, self._parse, self._mcp, self._ops, self._sources,
                        self._raw, self._flag):
            patcher.stop()

    def test_no_planted_secret_appears_anywhere_in_the_packet(self):
        """The one that matters. The packet IS what gets persisted to chat_sessions.json."""
        packet = inv.investigate(ALERT)
        blob = json.dumps(packet, ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, blob, f"{secret} leaked into the packet")

    def test_the_packet_still_carries_usable_evidence(self):
        """Redaction that destroys the evidence is not a win; the exception classes must survive."""
        packet = inv.investigate(ALERT)
        self.assertTrue(packet["evidence"])
        item = packet["evidence"][0]
        self.assertIn("SmsDeliveryException", item["exception_classes"])
        self.assertEqual(item["lines_seen"], 6)
        self.assertEqual(item["lines_returned"], 5)          # capped, and the cap is stated
        self.assertTrue(packet["redactions"])

    def test_production_data_is_labelled_and_the_storage_rule_is_stated(self):
        packet = inv.investigate(ALERT)
        self.assertTrue(packet["contains_production_data"])
        self.assertEqual(packet["evidence"][0]["environment"], "production")
        self.assertIn("never returned", packet["storage_rule"])
        self.assertIn("dev/SCT", packet["environments"]["route_snapshot"])

    def test_both_production_log_sources_are_queried_and_each_item_says_which(self):
        """Owner 2026-07-29: both LogDream sources are production and hold different logs. Asserted
        against `log_sources()` rather than literals — the literal is what was wrong (`hk1` for
        `hkl`), so a test that repeats it just agrees with the bug."""
        inv.investigate(ALERT)
        sources = {args.get("source") for op, args in self.calls if op == "log.read"}
        self.assertEqual(sources, set(inv.log_sources()))

    def test_the_stamp_is_reformatted_but_the_moment_is_never_converted(self):
        """The real tool REJECTED `2026-07-30 03:15 HKT` and wants the zone as its own parameter
        (intranet, 2026-07-31). Reformatting is not converting: 03:15 is still 03:15."""
        inv.investigate(ALERT)
        reads = [args for op, args in self.calls if op == "log.read"]
        self.assertTrue(reads)
        # The real read_logdream_log has no from/to window: it backtracks from an alert time.
        self.assertEqual(reads[0]["alert_time"], "2026-07-30 03:15:00")
        self.assertEqual(reads[0]["timezone"], "Asia/Hong_Kong")
        self.assertNotIn("HKT", reads[0]["alert_time"])       # the zone never rides along
        self.assertIn("03:15", reads[0]["alert_time"])        # and the hour did not move
        self.assertEqual(reads[0]["mode"], inv.READ_MODE_BACKTRACK)
        self.assertNotIn("from_time", reads[0])

    def test_an_app_name_the_server_does_not_know_is_not_queried(self):
        """Querying a guessed app returns an empty result that reads exactly like 'no problem'."""
        def _fake_call(operation, args=None, **_kw):
            self.calls.append((operation, dict(args or {})))
            if operation == "log.list_apps":
                return {"ok": True, "text": '["somethingElse"]'}
            raise AssertionError("log.read must not run for an unverified app name")
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertTrue(any("do not exist on the server" in n or "none of the candidate" in n
                            for n in packet["not_investigated"]))
        self.assertFalse(packet["contains_production_data"])

    def test_a_server_that_did_not_respond_is_not_reported_as_nothing_found(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            raise mcp_client.TransportError("LogDream unreachable")
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertTrue(packet["not_investigated"])
        self.assertIn("NOT evidence of no matching lines", " ".join(packet["not_investigated"]))

    def test_with_the_mcp_flag_off_it_says_so_instead_of_failing(self):
        with mock.patch.object(config, "MCP_ENABLED", False):
            packet = inv.investigate(ALERT)
        self.assertFalse(packet["contains_production_data"])
        self.assertIn("SDLC_MCP_ENABLED", " ".join(packet["not_investigated"]))
        self.assertEqual(packet["evidence"], [])

    def test_an_unwired_log_operation_stops_the_run_and_says_which(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            raise mcp_registry.NotWired("log.read cannot pass 'mode' yet")
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertIn("not fully wired", " ".join(packet["caveats"]))

    def test_the_query_budget_is_bounded_and_says_what_it_skipped(self):
        """8 keywords x 2 production sources is 16 calls, and RUNBOOK-55 clocked one at 26.4s. The
        cap is the difference between a slow answer and a seven-minute one — but a silent stop would
        read as 'those keywords found nothing', so what was skipped has to be named."""
        hits = ["a/B.java:1: throw new SmsDeliveryException(m);",
                "a/C.java:1: throw new VendorTimeoutException(m);"]
        with mock.patch.object(inv.rcode, "search_code", lambda *a, **k: hits), \
             mock.patch.object(inv, "_MAX_LOG_QUERIES", 3):
            packet = inv.investigate(ALERT)
        self.assertEqual(len(packet["queries_executed"]), 3)      # 3 keywords x 2 sources, capped at 3
        skipped = " ".join(packet["not_investigated"])
        self.assertIn("query budget", skipped)
        self.assertIn("do NOT read this as", skipped)
        self.assertIn("hkp3", skipped)                       # names the pairs it never tried

    def test_queries_executed_records_exactly_what_was_spent(self):
        packet = inv.investigate(ALERT)
        self.assertTrue(packet["queries_executed"])
        for entry in packet["queries_executed"]:
            self.assertEqual(entry["app"], "cslSmsDeli")
            self.assertIn(entry["source"], inv.log_sources())
            self.assertTrue(entry["keyword"])

    def test_a_nil_result_is_scoped_to_the_keywords_actually_used(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            return {"ok": True, "text": "   "}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertIn("real finding only for the keywords", " ".join(packet["caveats"]))
        self.assertTrue(packet["plan"]["keywords"])


class StreamingTests(InvestigateTests):
    """Progress events are streamed straight to a browser, so they are the one place a redaction
    miss would be visible before anyone could review it. Inherits the fixtures above."""

    def _events(self, **kw):
        return list(inv.investigate_events(ALERT, **kw))

    def test_no_planted_secret_appears_in_any_streamed_event(self):
        blob = json.dumps([e for e in self._events() if e.get("type") == "subagent_step"],
                          ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, blob, f"{secret} leaked into a progress event")

    def test_the_stream_never_carries_a_log_line_even_a_redacted_one(self):
        """Excerpts belong in the packet, which the model reads. The live feed needs counts only."""
        for event in self._events():
            if event.get("type") != "subagent_step":
                continue
            self.assertNotIn("excerpts", event["detail"])
            self.assertNotIn("ERROR", event["label"])

    def test_the_steps_narrate_plan_then_apps_then_queries_then_summary(self):
        steps = [e["step"] for e in self._events() if e.get("type") == "subagent_step"]
        self.assertEqual(steps[0], "plan")
        self.assertEqual(steps[-1], "summary")
        for expected in ("plan_done", "apps", "apps_done", "app_resolved", "query", "evidence"):
            self.assertIn(expected, steps)

    def test_a_query_step_names_the_app_source_and_keyword_being_spent(self):
        """An opaque spinner is what makes people distrust an agent that is working correctly."""
        query = next(e for e in self._events() if e.get("step") == "query")
        self.assertEqual(query["detail"]["app"], "cslSmsDeli")
        self.assertIn(query["detail"]["source"], inv.log_sources())
        self.assertTrue(query["detail"]["keyword"])
        self.assertIn(query["detail"]["keyword"], query["label"])

    def test_every_external_call_step_names_the_mcp_server_and_operation(self):
        """Ops need to see that a step was a call INTO LogDream, not a read of our own index —
        it is the first thing you check when a step looks wrong."""
        called = [e for e in self._events()
                  if e.get("type") == "subagent_step" and (e["detail"].get("server"))]
        self.assertTrue(called)
        for event in called:
            self.assertEqual(event["detail"]["server"], "logdream")
            self.assertIn(event["detail"]["operation"],
                          ("log.list_apps", "log.search_files", "log.read"))

    def test_local_steps_carry_no_mcp_badge(self):
        """Reading the alert and building the plan touch nothing external; claiming otherwise would
        make the badge meaningless."""
        for step in ("plan", "plan_done", "app_resolved", "summary"):
            event = next((e for e in self._events() if e.get("step") == step), None)
            if event:
                self.assertNotIn("server", event["detail"], step)

    def test_call_latency_is_reported_so_a_slow_endpoint_is_visible(self):
        hit = next(e for e in self._events() if e.get("step") == "evidence")
        self.assertIn("elapsed_ms", hit["detail"])

    def test_a_local_refusal_is_marked_as_never_having_left_the_process(self):
        """"nobody finished wiring this" and "the log service is down" escalate to different people."""
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            raise mcp_registry.NotWired("log.read cannot pass 'mode' yet")
        with mock.patch.object(mcp_client, "call", _fake_call):
            event = next(e for e in inv.investigate_events(ALERT) if e.get("step") == "unwired")
        self.assertTrue(event["detail"]["refused_locally"])
        self.assertIn("未发出请求", event["label"])

    def test_an_evidence_step_reports_counts_and_exception_classes_only(self):
        hit = next(e for e in self._events() if e.get("step") == "evidence")
        self.assertEqual(hit["detail"]["lines_seen"], 6)
        self.assertIn("SmsDeliveryException", hit["detail"]["exception_classes"])
        self.assertEqual(set(hit["detail"]) & {"text", "excerpts", "content"}, set())

    def test_the_terminal_event_carries_the_same_packet_the_tool_returns(self):
        events = self._events()
        streamed = next(e for e in events if e.get("type") == "result")["packet"]
        self.assertEqual(streamed, inv.investigate(ALERT))

    def test_a_refusal_is_streamed_as_a_stop_not_as_silence(self):
        with mock.patch.object(inv.incident, "parse_alert",
                               lambda *a, **k: {"identified": False, "repos": [], "use_cases": [],
                                                "times": [], "metric": "", "notes": []}):
            steps = [e["step"] for e in inv.investigate_events("something broke")
                     if e.get("type") == "subagent_step"]
        self.assertIn("refused", steps)

    def test_the_flag_being_off_is_streamed_too(self):
        with mock.patch.object(config, "MCP_ENABLED", False):
            steps = [e["step"] for e in self._events() if e.get("type") == "subagent_step"]
        self.assertIn("disabled", steps)


class DrillDownTests(InvestigateTests):
    """Follow-up questions: narrow, widen, or re-aim without starting over."""

    def test_supplied_keywords_replace_the_derived_list(self):
        """"search for X instead" must spend the budget on X, not bury it behind six derived terms."""
        packet = inv.investigate(ALERT, keywords=["ConnectException", "SocketTimeout"])
        terms = [k["term"] for k in packet["plan"]["keywords"]]
        self.assertEqual(terms, ["ConnectException", "SocketTimeout"])
        self.assertNotIn("CPUUtilization", terms)

    def test_supplied_keywords_are_marked_as_not_derived_from_the_graph(self):
        """A nil result on a graph-derived list means far more than one on a guessed term."""
        packet = inv.investigate(ALERT, keywords=["ConnectException"])
        self.assertIn("not derived from the code graph", packet["plan"]["keywords"][0]["why"])
        self.assertIn("only speaks to the terms the user asked for",
                      packet["plan"]["keywords_note"])

    def test_narrowing_sources_is_honoured_and_flagged_as_covering_less(self):
        packet = inv.investigate(ALERT, sources=["hkp3"])
        self.assertEqual({q["source"] for q in packet["queries_executed"]}, {"hkp3"})
        self.assertIn("BOTH production", packet["plan"]["sources_note"])

    def test_raising_the_query_budget_lets_a_wider_sweep_run(self):
        hits = ["a/B.java:1: throw new SmsDeliveryException(m);",
                "a/C.java:1: throw new VendorTimeoutException(m);"]
        with mock.patch.object(inv.rcode, "search_code", lambda *a, **k: hits):
            narrow = inv.investigate(ALERT, max_queries=2)
            wide = inv.investigate(ALERT, max_queries=6)
        self.assertEqual(len(narrow["queries_executed"]), 2)
        self.assertEqual(len(wide["queries_executed"]), 6)
        self.assertTrue(narrow["not_investigated"])          # says what it skipped
        self.assertIn("2-read query budget", " ".join(narrow["not_investigated"]))

    def test_the_default_budget_still_applies_when_no_override_is_given(self):
        with mock.patch.object(inv, "_MAX_LOG_QUERIES", 3):
            packet = inv.investigate(ALERT)
        self.assertLessEqual(len(packet["queries_executed"]), 3)

    def test_blank_keywords_fall_back_to_the_derived_list(self):
        """Zero keywords would mean zero queries — an investigation that searched nothing while
        looking like it ran."""
        packet = inv.investigate(ALERT, keywords=["", "  "])
        self.assertNotIn("keywords_note", packet["plan"])
        self.assertIn("CPUUtilization", [k["term"] for k in packet["plan"]["keywords"]])
        self.assertTrue(packet["queries_executed"])

    def test_blank_sources_fall_back_to_both_production_sources(self):
        packet = inv.investigate(ALERT, sources=[" "])
        self.assertEqual({q["source"] for q in packet["queries_executed"]}, set(inv.log_sources()))
        self.assertNotIn("sources_note", packet["plan"])


class AgentRelayTests(unittest.TestCase):
    """The agent loop must forward sub-agent steps and keep only the terminal packet."""

    def test_dispatch_events_yields_progress_then_one_result(self):
        from webapp import tools
        events = list(tools.dispatch_events("incident_investigate", {"alert_text": "x"}))
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(sum(1 for e in events if e["type"] == "result"), 1)

    def test_a_non_subagent_tool_yields_exactly_one_result(self):
        """So callers need no special case."""
        from webapp import tools
        events = list(tools.dispatch_events("hubs", {"top": 1}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "result")

    def test_a_blank_alert_still_produces_a_result_event(self):
        from webapp import tools
        events = list(tools.dispatch_events("incident_investigate", {"alert_text": " "}))
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["packet"]["ok"])

    def test_the_agent_relays_steps_and_tags_them_with_the_agent_name(self):
        from webapp import agent, tools
        fake = [{"type": "subagent_step", "step": "plan", "label": "读告警", "detail": {}},
                {"type": "result", "packet": {"ok": True, "evidence": []}}]
        calls = [{"id": "c1", "type": "function",
                  "function": {"name": "incident_investigate",
                               "arguments": '{"alert_text": "x"}'}}]
        replies = iter([{"role": "assistant", "content": None, "tool_calls": calls},
                        {"role": "assistant", "content": "done"}])

        def _chat(messages, tool_list=None):
            yield ("final", next(replies))

        with mock.patch.object(agent.llm, "chat_stream", _chat), \
             mock.patch.object(agent.llm, "stream_text", lambda m: []), \
             mock.patch.object(tools, "dispatch_events", lambda n, a, owner="": iter(fake)):
            events = list(agent.answer_events("q"))
        relayed = [e for e in events if e.get("type") == "subagent_step"]
        self.assertEqual(len(relayed), 1)
        self.assertEqual(relayed[0]["agent"], "incident_investigate")
        self.assertEqual(relayed[0]["label"], "读告警")


class ToolErrorIsNeverEvidenceTests(InvestigateTests):
    """Reported by the intranet 2026-07-30, and the most dangerous defect this feature has had.

    An MCP call has FOUR outcomes: transport failure (raised), the tool running and reporting failure,
    the tool succeeding with nothing, and the tool succeeding with content. Reading `text`
    unconditionally collapses the second into the fourth — and a tool's error body is non-empty, so
    "unknown source hkl" would be wrapped up and presented as log evidence.
    """

    def test_a_tool_reported_error_from_log_read_never_becomes_evidence(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            return {"ok": False, "tool_reported_error": True, "text": "unknown source hkl"}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertFalse(packet["contains_production_data"])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("REPORTED AN ERROR", joined)
        self.assertIn("not a log finding", joined)
        self.assertNotIn("unknown source hkl", json.dumps(packet["evidence"]))

    def test_a_rejected_read_is_streamed_as_a_stop_not_as_a_hit(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            return {"ok": False, "tool_reported_error": True, "text": "unknown source hkl"}
        with mock.patch.object(mcp_client, "call", _fake_call):
            steps = [e["step"] for e in inv.investigate_events(ALERT)
                     if e.get("type") == "subagent_step"]
        self.assertIn("query_rejected", steps)
        self.assertNotIn("evidence", steps)

    def test_an_error_body_from_list_apps_never_becomes_app_names(self):
        """It would otherwise be split on whitespace and every token treated as an app."""
        def _fake_call(operation, args=None, **_kw):
            return {"ok": False, "tool_reported_error": True,
                    "text": "unknown source hk1 - valid sources are hkl hkp3"}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("REJECTED by LogDream", joined)
        self.assertIn("half the log coverage", joined)

    def test_a_source_rejected_on_listing_is_dropped_and_the_other_still_runs(self):
        """A wrong source name must cost that source, not the whole investigation."""
        def _fake_call(operation, args=None, **_kw):
            source = (args or {}).get("source")
            if operation == "log.list_apps":
                if source == inv.log_sources()[0]:
                    return {"ok": False, "tool_reported_error": True, "text": "unknown source"}
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            return {"ok": True, "text": DIRTY_LOG}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        searched = {q["source"] for q in packet["queries_executed"]}
        self.assertEqual(searched, {inv.log_sources()[1]})
        self.assertTrue(packet["evidence"])
        self.assertIn("REJECTED", " ".join(packet["not_investigated"]))

    def test_the_outcome_helper_separates_all_four_cases(self):
        self.assertEqual(inv._tool_outcome({"ok": True, "text": "lines"}), ("hit", "lines"))
        self.assertEqual(inv._tool_outcome({"ok": True, "text": "  "}), ("empty", ""))
        self.assertEqual(inv._tool_outcome({"ok": False, "text": "boom"}), ("error", "boom"))
        self.assertEqual(
            inv._tool_outcome({"ok": True, "tool_reported_error": True, "text": "boom"}),
            ("error", "boom"))
        self.assertEqual(inv._tool_outcome(None)[0], "error")


class LogSourceResolutionTests(unittest.TestCase):
    """`hk1` (digit one) vs `hkl` (letter L), reported by the intranet 2026-07-30. Source names are
    ENVIRONMENT vocabulary, so the config owns them and Python only holds the fallback."""

    def test_the_default_source_is_the_letter_l_not_a_digit_one(self):
        self.assertEqual(inv.DEFAULT_LOG_SOURCES, ("hkl", "hkp3"))
        self.assertNotIn("hk1", inv.DEFAULT_LOG_SOURCES)

    def test_sources_come_from_the_intranet_config_when_declared(self):
        """Hard-coding them was the RUNBOOK-49/50/51 mistake again: environment vocabulary in Python
        needs a push to fix, and the box cannot push."""
        with mock.patch.object(inv.mcp_registry, "servers", lambda cfg=None: {
                "logdream": {"sources": {"alpha": {"query_by_default": True},
                                          "beta": {"query_by_default": False},
                                          "_note": "doc key"}}}):
            self.assertEqual(inv.log_sources(), ("alpha",))

    def test_a_config_without_sources_falls_back_to_the_default(self):
        for servers in ({"logdream": {}}, {}, {"logdream": {"sources": {}}}):
            with mock.patch.object(inv.mcp_registry, "servers", lambda cfg=None, s=servers: s):
                self.assertEqual(inv.log_sources(), inv.DEFAULT_LOG_SOURCES)


class SourceHandlingTests(InvestigateTests):
    """`log.list_apps` needs a `source`, and the two sources hold different app lists."""

    def test_list_apps_is_called_once_per_source_with_the_source_argument(self):
        """The real `list_logdream_apps` requires it, and the two sources hold DIFFERENT apps."""
        inv.investigate(ALERT)
        listings = [args for op, args in self.calls if op == "log.list_apps"]
        self.assertEqual(len(listings), len(inv.log_sources()))
        self.assertEqual({a.get("source") for a in listings}, set(inv.log_sources()))

    def test_an_app_present_on_only_one_source_is_queried_only_there(self):
        """Querying a source that has never heard of the app returns empty — which reads as
        'no problem'."""
        only = inv.log_sources()[1]
        def _fake_call(operation, args=None, **_kw):
            source = (args or {}).get("source")
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]' if source == only else '["other"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            return {"ok": True, "text": DIRTY_LOG}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual({q["source"] for q in packet["queries_executed"]}, {only})
        self.assertIn("but NOT on", " ".join(packet["not_investigated"]))

    def test_source_is_omitted_when_the_operation_does_not_map_it(self):
        """Schema-flexible: the committed config declares no args for log.list_apps, and the box's
        local one does. Neither should crash."""
        with mock.patch.object(inv.mcp_registry, "operations",
                               lambda cfg=None: {"log.list_apps": {"args": {}}}):
            inv.investigate(ALERT)
        listings = [args for op, args in self.calls if op == "log.list_apps"]
        self.assertTrue(listings)
        self.assertEqual([a.get("source") for a in listings], [None] * len(listings))


class FileSelectionTests(unittest.TestCase):
    """`select_log_files` — parsing only, no sockets.

    The rule it enforces: an empty candidate list ends in "we could not identify a log file", never
    in a guessed name. Hard-coding `otx_trace.log` was exactly that guess, and it ALSO mislabelled
    evidence whenever the real read was something else.
    """

    def test_a_json_list_of_paths_is_parsed_and_ranked(self):
        got = inv.select_log_files(json.dumps(
            ["/apps/x/log/otx_trace.log.20260701", "/apps/x/log/otx_trace.log"]), limit=2)
        self.assertEqual(got, ["/apps/x/log/otx_trace.log", "/apps/x/log/otx_trace.log.20260701"])

    def test_objects_with_a_name_field_are_parsed(self):
        got = inv.select_log_files(json.dumps(
            [{"file_name": "exception.log", "size": 12}, {"name": "otx_trace.log"}]), limit=2)
        self.assertEqual(sorted(got), ["exception.log", "otx_trace.log"])

    def test_plain_text_output_still_yields_file_names(self):
        got = inv.select_log_files("found:\n  otx_trace.log  (2MB)\n  sftp.log  (1MB)\n", limit=5)
        self.assertIn("otx_trace.log", got)
        self.assertIn("sftp.log", got)

    def test_the_alert_date_wins_over_the_preferred_type(self):
        got = inv.select_log_files(
            json.dumps(["otx_trace.log", "exception.log.20260730"]),
            alert_date="2026-07-30", limit=1)
        self.assertEqual(got, ["exception.log.20260730"])

    def test_nothing_recognisable_yields_nothing_rather_than_a_guess(self):
        for text in ("", "no files here", json.dumps({"error": "bad app"})):
            self.assertEqual(inv.select_log_files(text), [], repr(text))

    def test_the_candidate_list_is_bounded(self):
        many = json.dumps([f"otx_trace.log.2026070{i}" for i in range(9)])
        self.assertLessEqual(len(inv.select_log_files(many)), inv._MAX_FILES_PER_SOURCE)


class SearchFilesHopTests(InvestigateTests):
    """The hop that was missing entirely: the real read tool REQUIRES a file name."""

    def test_search_files_runs_before_any_read(self):
        inv.investigate(ALERT)
        order = [op for op, _ in self.calls]
        self.assertIn("log.search_files", order)
        self.assertLess(order.index("log.search_files"), order.index("log.read"))

    def test_the_real_selected_file_name_is_passed_to_read(self):
        inv.investigate(ALERT)
        reads = [args for op, args in self.calls if op == "log.read"]
        self.assertTrue(reads)
        for args in reads:
            self.assertIn("file", args)
            # A rotated file (`otx_trace.log.20260701`) is a real candidate, so match on `.log`
            # appearing rather than on the name ending there.
            self.assertIn(".log", args["file"])

    def test_evidence_records_the_file_actually_read_not_a_hard_coded_name(self):
        """Mislabelling `exception.log` as `otx_trace.log` misdirects whoever goes to check it."""
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["exception.log"]'}
            return {"ok": True, "text": DIRTY_LOG}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertTrue(packet["evidence"])
        self.assertEqual({item["file"] for item in packet["evidence"]}, {"exception.log"})

    def test_no_candidate_file_means_nothing_is_read(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": "no matching files"}
            raise AssertionError("log.read must not run without a file name")
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertIn("never guessed", " ".join(packet["not_investigated"]))

    def test_a_file_search_error_is_not_treated_as_a_file_list(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": False, "tool_reported_error": True,
                        "text": "app not found: cslSmsDeli.log"}
            raise AssertionError("log.read must not run after a failed file search")
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertIn("file-search tool REPORTED AN ERROR", " ".join(packet["not_investigated"]))

    def test_read_is_refused_locally_when_the_config_cannot_pass_a_file_name(self):
        """Before the intranet maps `file`, a read cannot possibly succeed — so it is not sent, and
        the message names exactly which mapping is missing."""
        with mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.list_apps": {"args": {"source": "source"}},
                "log.search_files": {"args": {"app": "app", "source": "source"}},
                "log.read": {"args": {"app": "app", "source": "source", "file": "?"}}}):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertEqual([op for op, _ in self.calls].count("log.read"), 0)
        joined = " ".join(packet["not_investigated"])
        self.assertIn("does not map", joined)
        self.assertIn("file", joined)


class QueryAccountingTests(InvestigateTests):
    """attempted / executed / failed. One list written before the request made a locally-refused
    call look queried — "we asked and found nothing" and "we never asked" became the same thing."""

    def test_a_successful_read_is_counted_as_executed(self):
        packet = inv.investigate(ALERT)
        self.assertTrue(packet["queries_executed"])
        self.assertEqual(len(packet["queries_attempted"]), len(packet["queries_executed"]))
        self.assertEqual(packet["queries_failed"], [])

    def test_a_locally_refused_read_is_attempted_but_never_executed(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            raise mcp_registry.NotWired("log.read cannot pass 'mode' yet")
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertTrue(packet["queries_attempted"])
        self.assertEqual(packet["queries_executed"], [])
        self.assertTrue(packet["queries_failed"][0]["refused_locally"])

    def test_a_tool_error_counts_as_executed_and_failed(self):
        """It DID reach the server — that is a different fact from never having asked."""
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
            if operation == "log.search_files":
                return {"ok": True, "text": '["otx_trace.log"]'}
            return {"ok": False, "tool_reported_error": True, "text": "bad file"}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertTrue(packet["queries_executed"])
        self.assertTrue(packet["queries_failed"])
        self.assertFalse(packet["queries_failed"][0]["refused_locally"])
        self.assertEqual(packet["evidence"], [])

    def test_every_attempt_records_which_args_were_actually_sent(self):
        packet = inv.investigate(ALERT)
        for entry in packet["queries_attempted"]:
            self.assertIn("file", entry["args_sent"])
            self.assertIn("app", entry["args_sent"])


class ArgumentContractTests(InvestigateTests):
    """Only args the config maps are sent, and a `const` the box pinned is never fought."""

    def test_unmapped_args_are_dropped_rather_than_sent(self):
        with mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.list_apps": {"args": {"source": "source"}},
                "log.search_files": {"args": {"app": "app", "source": "source"}},
                "log.read": {"args": {"app": "app", "source": "source", "file": "file_name"}}}):
            inv.investigate(ALERT)
        reads = [args for op, args in self.calls if op == "log.read"]
        self.assertTrue(reads)
        self.assertEqual(set(reads[0]), {"app", "source", "file"})
        self.assertNotIn("alert_time", reads[0])

    def test_a_const_pinned_by_the_box_is_not_overridden(self):
        """`const` is the intranet's override channel; sending our own value would silently beat it."""
        with mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.list_apps": {"args": {"source": "source"}},
                "log.search_files": {"args": {"app": "app", "source": "source"}},
                "log.read": {"args": {"app": "app", "source": "source", "file": "file_name",
                                       "mode": "read_mode"},
                              "const": {"read_mode": "pinned_by_intranet"}}}):
            inv.investigate(ALERT)
        reads = [args for op, args in self.calls if op == "log.read"]
        self.assertNotIn("mode", reads[0])

    def test_a_placeholder_mapping_counts_as_unusable(self):
        with mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.read": {"args": {"app": "app", "source": "?", "file": "file_name"}}}):
            self.assertEqual(inv._usable_args("log.read"), {"app", "file"})


class StructuredResponseTests(unittest.TestCase):
    """Reported by the intranet 2026-07-31: three parsers read a structured JSON body as text.

    One root cause, three symptoms. The body arrives as JSON; splitting it counts JSON source lines
    and turns JSON keys into data. The rule now: a body that parses as JSON is read structurally,
    and a shape we cannot read fails closed instead of scraping tokens out of it.
    """

    # The intranet's synthetic: two log lines, pretty-printed to ~11 lines of JSON source. The old
    # `raw.splitlines()` reported 11.
    TWO_LINES = json.dumps({
        "app": "cslSmsDeli",
        "file": "otx_trace.log",
        "line_count": 2,
        "lines": [
            "2026-07-30 03:15:01 ERROR SmsDeliveryException failed",
            "2026-07-30 03:15:02 ERROR TimeoutException gave up",
        ],
    }, indent=2)
    # One directory (the app) and one file beside it. The old regex split produced seven "apps":
    # README.txt, cslSmsDeli, dir, entries, entry_type, file, name.
    APP_LISTING = json.dumps({"entries": [
        {"name": "cslSmsDeli", "entry_type": "dir"},
        {"name": "README.txt", "entry_type": "file"},
    ]})

    def test_a_structured_two_line_response_counts_two_lines_not_eleven(self):
        self.assertGreater(len(self.TWO_LINES.splitlines()), 2)      # the trap is really there
        lines, reported, error = inv.extract_log_lines(self.TWO_LINES)
        self.assertEqual(error, "")
        self.assertEqual(len(lines), 2)
        self.assertEqual(reported, 2)
        self.assertNotIn("line_count", " ".join(lines))

    def test_line_objects_are_read_by_their_text_field(self):
        body = json.dumps({"lines": [{"line": "ERROR one", "ts": 1}, {"line": "ERROR two", "ts": 2}]})
        lines, _reported, error = inv.extract_log_lines(body)
        self.assertEqual(lines, ["ERROR one", "ERROR two"])
        self.assertEqual(error, "")

    def test_plain_log_text_still_takes_the_legacy_split(self):
        lines, reported, error = inv.extract_log_lines(DIRTY_LOG)
        self.assertEqual(len(lines), 6)
        self.assertIsNone(reported)
        self.assertEqual(error, "")

    def test_an_unreadable_json_body_fails_closed_rather_than_being_split(self):
        """The whole point: an unknown shape must produce NOTHING, and say what it looked for."""
        for body in (json.dumps({"result": {"payload": "x"}}),
                     json.dumps({"lines": {"not": "a list"}}),
                     json.dumps([{"no_text_field": 1}])):
            lines, _reported, error = inv.extract_log_lines(body)
            self.assertIsNone(lines, body)
            self.assertIn("mcp_tools.json", error)

    def test_structured_list_apps_accepts_only_directory_names(self):
        names, note, error = inv.extract_app_names(self.APP_LISTING)
        self.assertEqual(error, "")
        self.assertEqual(names, ["cslSmsDeli"])
        for token in ("README.txt", "dir", "entries", "entry_type", "file", "name"):
            self.assertNotIn(token, names, token)
        self.assertIn("files are not apps", note)

    def test_a_legacy_list_of_strings_is_still_accepted(self):
        names, _note, error = inv.extract_app_names('["cslSmsDeli", "otherApp"]')
        self.assertEqual(names, ["cslSmsDeli", "otherApp"])
        self.assertEqual(error, "")

    def test_entries_without_a_kind_field_are_accepted_but_said_so(self):
        """Requiring a field no server has been observed to send would refuse every real app —
        its own silent outage. Accepting them is fine; leaving it unsaid is not."""
        names, note, error = inv.extract_app_names(json.dumps({"apps": [{"name": "cslSmsDeli"}]}))
        self.assertEqual((names, error), (["cslSmsDeli"], ""))
        self.assertIn("no entry-type field", note)

    def test_an_unreadable_app_listing_yields_no_names_rather_than_json_tokens(self):
        for body in (json.dumps({"payload": {"deep": ["cslSmsDeli"]}}),
                     json.dumps({"entries": "cslSmsDeli"}),
                     json.dumps([{"nope": 1}])):
            names, _note, error = inv.extract_app_names(body)
            self.assertIsNone(names, body)
            self.assertIn("mcp_tools.json", error)

    def test_structured_content_wins_over_the_text_block(self):
        """`structuredContent` is the server's own typed answer; mcp_client already carries it."""
        lines, _reported, _error = inv.extract_log_lines(
            "ignored text", structured={"lines": ["only real line"]})
        self.assertEqual(lines, ["only real line"])

    def test_search_files_does_not_fall_back_to_regex_on_a_json_body(self):
        """A JSON body whose shape we do not recognise must not be mined for `.log` substrings."""
        self.assertEqual(
            inv.select_log_files(json.dumps({"error": "otx_trace.log is not readable"})), [])

    def test_the_response_shape_is_overridable_from_the_intranet_config(self):
        """The field names are THEIR environment, so fixing one must be a config edit on the box —
        not a push from outside. Dotted paths so a nested body needs no code change either."""
        with mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.read": {"response": {"lines": "data.rows", "line_text": "msg"}}}):
            lines, _reported, error = inv.extract_log_lines(
                json.dumps({"data": {"rows": [{"msg": "ERROR one"}]}}))
        self.assertEqual((lines, error), (["ERROR one"], ""))


class ShapeProbeTests(unittest.TestCase):
    """`describe_shape` / `describe_response` — how the intranet answers "what does your tool return?"
    without a production response leaving the log host, and without anyone reading JSON by eye.

    It is a diagnostic that runs against PRODUCTION responses, so the leak test applies to it just
    as much as to the evidence packet.
    """

    def test_no_value_ever_appears_in_a_shape(self):
        body = {"lines": [{"line": "customer alice.wong@example.com 9123 4567", "level": "ERROR"}],
                "line_count": 1, "app": "cslSmsDeli"}
        blob = json.dumps(inv.describe_shape(body), ensure_ascii=False)
        for secret in ("alice.wong@example.com", "9123 4567", "ERROR", "cslSmsDeli"):
            self.assertNotIn(secret, blob, secret)
        self.assertIn("lines", blob)          # names DO survive; that is the entire point
        self.assertIn("line_count", blob)

    def test_a_field_name_that_is_itself_data_is_redacted(self):
        """A body keyed by account number would otherwise leak through the one field this has to
        print."""
        blob = json.dumps(inv.describe_shape({"4123456789012345": {"hits": 3}}))
        self.assertNotIn("4123456789012345", blob)

    def test_the_shape_names_types_lengths_and_nesting(self):
        shape = inv.describe_shape({"data": {"rows": ["a", "bb"]}, "total": 2, "ok": True})
        self.assertEqual(shape["total"], "int")
        self.assertEqual(shape["ok"], "bool")
        self.assertEqual(shape["data"]["rows"], {"list[2] of str": "str(len=1)"})

    def test_deep_or_wide_bodies_are_bounded(self):
        deep = {}
        node = deep
        for _ in range(30):
            node["next"] = {}
            node = node["next"]
        self.assertIn("depth limit", json.dumps(inv.describe_shape(deep)))
        wide = inv.describe_shape({f"k{i}": 1 for i in range(60)})
        self.assertTrue(any("more keys" in key for key in wide))

    def test_describe_response_says_what_they_sent_and_what_we_read(self):
        out = {"ok": True, "text": StructuredResponseTests.TWO_LINES}
        report = inv.describe_response(out, "log.read")
        self.assertTrue(report["body_is_json"])
        self.assertEqual(report["parsed"]["lines"], 2)
        self.assertEqual(report["parsed"]["server_reported_count"], 2)
        self.assertIn("lines", report["declared_shape"])
        self.assertNotIn("SmsDeliveryException", json.dumps(report))   # no log text, even here

    def test_describe_response_on_an_unreadable_body_names_the_fields_it_looked_for(self):
        report = inv.describe_response({"ok": True, "text": json.dumps({"payload": {"x": [1]}})},
                                       "log.read")
        self.assertIsNone(report["parsed"]["lines"])
        self.assertIn("mcp_tools.json", report["parsed"]["error"])
        self.assertIn("payload", json.dumps(report["shape"]))          # so they can see the real one

    def test_describe_response_handles_a_tool_error_and_plain_text(self):
        err = inv.describe_response({"ok": False, "text": "unknown source hkl"}, "log.list_apps")
        self.assertEqual(err["outcome"], "error")
        self.assertFalse(err["body_is_json"])


class StructuredResponseInvestigationTests(InvestigateTests):
    """The same three defects, end to end through `investigate` rather than the parsers alone."""

    def _calls(self, list_apps, search_files, read):
        def _fake_call(operation, args=None, **_kw):
            self.calls.append((operation, dict(args or {})))
            if operation == "log.list_apps":
                return {"ok": True, "text": list_apps}
            if operation == "log.search_files":
                return {"ok": True, "text": search_files}
            return {"ok": True, "text": read}
        return mock.patch.object(mcp_client, "call", _fake_call)

    def test_a_structured_read_reports_the_real_line_count_in_the_packet(self):
        with self._calls(StructuredResponseTests.APP_LISTING,
                         json.dumps(["otx_trace.log"]),
                         StructuredResponseTests.TWO_LINES):
            packet = inv.investigate(ALERT)
        self.assertTrue(packet["evidence"])
        self.assertEqual(packet["evidence"][0]["lines_seen"], 2)
        self.assertIn("SmsDeliveryException", packet["evidence"][0]["exception_classes"])
        # And none of the JSON scaffolding became an excerpt.
        self.assertNotIn("line_count", json.dumps(packet["evidence"][0]))

    def test_a_structured_app_listing_only_verifies_the_directory_entry(self):
        with self._calls(StructuredResponseTests.APP_LISTING,
                         json.dumps(["otx_trace.log"]),
                         StructuredResponseTests.TWO_LINES):
            packet = inv.investigate(ALERT)
        self.assertTrue(packet["queries_executed"])
        self.assertEqual({q["app"] for q in packet["queries_executed"]}, {"cslSmsDeli"})

    def test_an_unreadable_read_response_is_not_reported_as_nothing_found(self):
        """The query SUCCEEDED. Calling our own parse failure 'no matching lines' is the same
        family of lie as reporting a tool error as evidence."""
        with self._calls(StructuredResponseTests.APP_LISTING,
                         json.dumps(["otx_trace.log"]),
                         json.dumps({"unknown_shape": {"x": 1}})):
            events = list(inv.investigate_events(ALERT))
        packet = events[-1]["packet"]
        self.assertEqual(packet["evidence"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("The query SUCCEEDED", joined)
        self.assertIn("do not report it as 'nothing found'", joined)
        self.assertIn("query_unreadable", [e.get("step") for e in events])

    def test_an_unreadable_app_listing_stops_that_source_instead_of_guessing(self):
        with self._calls(json.dumps({"payload": ["cslSmsDeli"]}),
                         json.dumps(["otx_trace.log"]),
                         DIRTY_LOG):
            packet = inv.investigate(ALERT)
        self.assertEqual(packet["evidence"], [])
        self.assertEqual([op for op, _ in self.calls if op == "log.read"], [])
        self.assertIn("no recognised entries field", " ".join(packet["not_investigated"]))


class UnrunnableWindowIsFailClosedTests(unittest.TestCase):
    """Two rounds of intranet findings, same gate.

    2026-07-31 (a): `plan()` recorded the timezone refusal and then ran anyway.
    2026-07-31 (b): the real tool needs a full `alert_time` with the zone as a SEPARATE parameter,
    so a bare `03:15 HKT` is not runnable either — and guessing which day fails exactly like
    guessing the zone.

    Borrows `InvestigateTests`' fixtures without inheriting its tests: this class changes the alert
    the fixture parses, so re-running the happy-path suite under it would only assert that a
    deliberately unrunnable plan does not run.
    """

    tearDown = InvestigateTests.tearDown

    def setUp(self):
        InvestigateTests.setUp(self)
        self._repatch([{"text": "2026-07-30 03:15", "timezone": "", "ambiguous": True,
                        "normalized": "2026-07-30 03:15:00"}])

    def _repatch(self, times):
        self._parse.stop()
        self._parse = mock.patch.object(inv.incident, "parse_alert", lambda *a, **k: {
            "identified": True,
            "repos": [{"repo": "mc-hk-hase-csl-sms-deli-job", "confidence": "confirmed"}],
            "use_cases": [], "metric": "CPUUtilization", "notes": [], "environment": "prod",
            "times": [dict(t) for t in times]})
        self._parse.start()

    def test_the_plan_is_not_runnable_without_a_timezone(self):
        out = inv.plan(ALERT)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["window"])
        self.assertTrue(out["targets"])            # the service WAS identified; that is not enough
        self.assertIn("BLOCKING", " ".join(out["refusals"]))

    def test_an_unrunnable_window_makes_zero_mcp_calls(self):
        """The load-bearing assertion: not 'fewer calls', none."""
        packet = inv.investigate(ALERT)
        self.assertEqual(self.calls, [])
        self.assertEqual(packet["evidence"], [])
        self.assertFalse(packet["contains_production_data"])
        self.assertIn("NOTHING was queried", " ".join(packet["not_investigated"]))

    def test_the_refusal_names_the_window_as_the_blocker_not_the_service(self):
        """"we could not identify the service" would send the user to answer the wrong question."""
        step = next(e for e in inv.investigate_events(ALERT) if e.get("step") == "refused")
        self.assertIn("时间窗", step["label"])

    def test_supplying_the_timezone_makes_the_same_alert_runnable(self):
        packet = inv.investigate(ALERT, timezone="Asia/Hong_Kong")
        self.assertTrue(packet["queries_executed"])
        self.assertEqual(packet["plan"]["window"]["timezone"], "Asia/Hong_Kong")

    def test_a_clock_time_with_a_zone_but_no_date_is_also_refused(self):
        """`at 03:15 HKT` used to plan fine and then send a stamp the real tool rejects. Which DAY
        is a question, not a default."""
        self._repatch([{"text": "03:15 HKT", "timezone": "Asia/Hong_Kong", "normalized": ""}])
        packet = inv.investigate(ALERT)
        self.assertEqual(self.calls, [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("a DATE", joined)
        self.assertNotIn("a TIMEZONE", joined)     # the zone was there; do not ask for it again

    def test_the_caller_can_supply_the_missing_date(self):
        self._repatch([{"text": "03:15 HKT", "timezone": "Asia/Hong_Kong", "normalized": ""}])
        packet = inv.investigate(ALERT, alert_time="2026-07-30 03:15")
        reads = [args for op, args in self.calls if op == "log.read"]
        self.assertTrue(reads)
        self.assertEqual(reads[0]["alert_time"], "2026-07-30 03:15:00")
        self.assertIn("caller-supplied", packet["plan"]["window"]["source"])

    def test_a_refusal_names_both_halves_when_both_are_missing(self):
        self._repatch([])
        packet = inv.investigate(ALERT)
        joined = " ".join(packet["not_investigated"])
        self.assertIn("a DATE", joined)
        self.assertIn("a TIMEZONE", joined)
        self.assertIn("no timestamp at all", joined)

    def test_an_alert_with_a_full_stamp_and_zone_is_unaffected(self):
        self._repatch(ALERT_TIMES)
        self.assertTrue(inv.investigate(ALERT)["queries_executed"])


class AlertTimeFormatTests(unittest.TestCase):
    """`2026-07-30 03:15 HKT` was rejected by the real tool; the stamp and the zone are separate
    parameters (intranet, 2026-07-31)."""

    def test_normalization_is_a_reformat_not_a_conversion(self):
        from retriever import incident as ri
        for raw, want in (("2026-07-30 03:15", "2026-07-30 03:15:00"),
                          ("2026-07-30T03:15:00", "2026-07-30 03:15:00"),
                          ("2026-07-30T03:15:07Z", "2026-07-30 03:15:07")):
            self.assertEqual(ri.normalize_stamp(raw), want, raw)

    def test_a_clock_time_without_a_date_normalizes_to_nothing(self):
        """Not to today. Pairing a bare 03:15 with today's date is a guess with the same failure
        mode as guessing the zone."""
        from retriever import incident as ri
        for raw in ("03:15", "03:15:00", "", "not a time"):
            self.assertEqual(ri.normalize_stamp(raw), "", repr(raw))

    def test_the_parser_carries_the_normalized_stamp_alongside_the_verbatim_text(self):
        from retriever import incident as ri
        # `repos=` supplied because parse_alert returns before time extraction when the repo
        # universe is unavailable — which is the case in a checkout without index/repo_tags.json.
        times = ri.parse_alert("mc-hk-hase-csl-sms-deli-job broke at 2026-07-30 03:15 HKT",
                               repos=["mc-hk-hase-csl-sms-deli-job"])["times"]
        self.assertEqual(times[0]["text"], "2026-07-30 03:15 HKT")
        self.assertEqual(times[0]["normalized"], "2026-07-30 03:15:00")
        self.assertEqual(times[0]["timezone"], "Asia/Hong_Kong")

    def test_the_wire_format_is_overridable_from_the_intranet_config(self):
        """Their tool rejected one format once already; the next rename must not need a push."""
        with mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.read": {"request": {"alert_time_format": "%Y-%m-%dT%H:%M:%SZ"}}}):
            self.assertEqual(inv._format_alert_time("2026-07-30 03:15:00"),
                             "2026-07-30T03:15:00Z")

    def test_a_broken_format_string_keeps_the_stamp_rather_than_losing_it(self):
        with mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.read": {"request": {"alert_time_format": 12345}}}):
            self.assertEqual(inv._format_alert_time("2026-07-30 03:15:00"), "2026-07-30 03:15:00")


class ToolSurfaceTests(unittest.TestCase):
    def test_the_investigator_is_charged_to_the_subagent_lane(self):
        from webapp import tools
        self.assertIn("incident_investigate", tools.SUBAGENT_TOOLS)
        self.assertIn("incident_investigate",
                      {t["function"]["name"] for t in tools.TOOLS})

    def test_the_tool_requires_the_alert_text(self):
        from webapp import tools
        out = tools.dispatch("incident_investigate", {"alert_text": "  "})
        self.assertFalse(out["ok"])
        self.assertIn("alert_text is required", out["error"])


if __name__ == "__main__":
    unittest.main()
