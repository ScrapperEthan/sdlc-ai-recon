"""The incident investigator — the wall between production logs and persisted chat history.

The load-bearing tests are the leak tests: plant PII in a log response, run the investigator, and
assert it cannot be found anywhere in what comes back. The packet is exactly what gets persisted to
chat_sessions.json, so "not in the packet" is the property that matters, not "we redacted carefully".
"""
import json
import unittest
from unittest import mock

from webapp import config, incident_investigator as inv, mcp_client, mcp_registry


ALERT = "prodECS_mc-hk-hase-csl-sms-deli-job_service_CPUUtilizationMINOR[80percent] at 03:15 HKT"
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
        """Defence 2. Reaching it means a bug, so it must be counted, not fixed quietly."""
        packet, report = inv.sanitize_packet(
            {"evidence": [{"excerpts": ["leaked bob@example.com here"], "lines_seen": 1}]})
        self.assertEqual(report["sanitized_at_exit"], 1)
        self.assertEqual(report["kinds"], ["email"])
        self.assertNotIn("bob@example.com", json.dumps(packet))

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
        self.assertTrue(any("no time with an explicit timezone" in r for r in out["refusals"]))

    def test_an_explicit_zone_in_the_alert_is_used(self):
        with mock.patch.object(inv.incident, "parse_alert", self._parsed(
                ALERT, times=[{"text": "03:15 HKT", "timezone": "Asia/Hong_Kong"}])):
            out = inv.plan(ALERT)
        self.assertEqual(out["window"]["timezone"], "Asia/Hong_Kong")
        self.assertEqual(out["window"]["source"], "explicit in the alert text")

    def test_the_alerts_own_zone_beats_a_caller_supplied_one(self):
        with mock.patch.object(inv.incident, "parse_alert", self._parsed(
                ALERT, times=[{"text": "03:15 Z", "timezone": "UTC"}])):
            out = inv.plan(ALERT, timezone="Asia/Hong_Kong")
        self.assertEqual(out["window"]["timezone"], "UTC")

    def test_a_caller_supplied_zone_rescues_an_ambiguous_time(self):
        with mock.patch.object(inv.incident, "parse_alert", self._parsed(
                ALERT, times=[{"text": "03:15", "timezone": "", "ambiguous": True}])):
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
        self._ops = mock.patch.object(
            inv.mcp_registry, "operations",
            lambda cfg=None: {"log.list_apps": {"args": {"source": "source"}},
                              "log.read": {"args": {"app": "app", "source": "source",
                                                     "keyword": "keyword"}}})
        self._ops.start()
        self.calls = []

        def _fake_call(operation, args=None, **_kw):
            self.calls.append((operation, dict(args or {})))
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli", "otherApp"]'}
            return {"ok": True, "text": DIRTY_LOG}

        self._mcp = mock.patch.object(mcp_client, "call", _fake_call)
        self._mcp.start()
        self._parse = mock.patch.object(inv.incident, "parse_alert", lambda *a, **k: {
            "identified": True,
            "repos": [{"repo": "mc-hk-hase-csl-sms-deli-job", "confidence": "confirmed"}],
            "use_cases": [], "metric": "CPUUtilization", "notes": [], "environment": "prod",
            "times": [{"text": "03:15 HKT", "timezone": "Asia/Hong_Kong"}]})
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

    def test_the_window_is_passed_through_verbatim_and_never_converted(self):
        inv.investigate(ALERT)
        reads = [args for op, args in self.calls if op == "log.read"]
        self.assertTrue(reads)
        self.assertEqual(reads[0]["from_time"], "03:15 HKT")
        self.assertEqual(reads[0]["timezone"], "Asia/Hong_Kong")

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
        self.assertEqual(len(packet["queries_run"]), 3)      # 3 keywords x 2 sources, capped at 3
        skipped = " ".join(packet["not_investigated"])
        self.assertIn("query budget", skipped)
        self.assertIn("do NOT read this as", skipped)
        self.assertIn("hkp3", skipped)                       # names the pairs it never tried

    def test_queries_run_records_exactly_what_was_spent(self):
        packet = inv.investigate(ALERT)
        self.assertTrue(packet["queries_run"])
        for entry in packet["queries_run"]:
            self.assertEqual(entry["app"], "cslSmsDeli")
            self.assertIn(entry["source"], inv.log_sources())
            self.assertTrue(entry["keyword"])

    def test_a_nil_result_is_scoped_to_the_keywords_actually_used(self):
        def _fake_call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": '["cslSmsDeli"]'}
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
            self.assertIn(event["detail"]["operation"], ("log.list_apps", "log.read"))

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
        self.assertEqual({q["source"] for q in packet["queries_run"]}, {"hkp3"})
        self.assertIn("BOTH production", packet["plan"]["sources_note"])

    def test_raising_the_query_budget_lets_a_wider_sweep_run(self):
        hits = ["a/B.java:1: throw new SmsDeliveryException(m);",
                "a/C.java:1: throw new VendorTimeoutException(m);"]
        with mock.patch.object(inv.rcode, "search_code", lambda *a, **k: hits):
            narrow = inv.investigate(ALERT, max_queries=2)
            wide = inv.investigate(ALERT, max_queries=6)
        self.assertEqual(len(narrow["queries_run"]), 2)
        self.assertEqual(len(wide["queries_run"]), 6)
        self.assertTrue(narrow["not_investigated"])          # says what it skipped
        self.assertIn("2-read query budget", " ".join(narrow["not_investigated"]))

    def test_the_default_budget_still_applies_when_no_override_is_given(self):
        with mock.patch.object(inv, "_MAX_LOG_QUERIES", 3):
            packet = inv.investigate(ALERT)
        self.assertLessEqual(len(packet["queries_run"]), 3)

    def test_blank_keywords_fall_back_to_the_derived_list(self):
        """Zero keywords would mean zero queries — an investigation that searched nothing while
        looking like it ran."""
        packet = inv.investigate(ALERT, keywords=["", "  "])
        self.assertNotIn("keywords_note", packet["plan"])
        self.assertIn("CPUUtilization", [k["term"] for k in packet["plan"]["keywords"]])
        self.assertTrue(packet["queries_run"])

    def test_blank_sources_fall_back_to_both_production_sources(self):
        packet = inv.investigate(ALERT, sources=[" "])
        self.assertEqual({q["source"] for q in packet["queries_run"]}, set(inv.log_sources()))
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
            return {"ok": True, "text": DIRTY_LOG}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        searched = {q["source"] for q in packet["queries_run"]}
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
            return {"ok": True, "text": DIRTY_LOG}
        with mock.patch.object(mcp_client, "call", _fake_call):
            packet = inv.investigate(ALERT)
        self.assertEqual({q["source"] for q in packet["queries_run"]}, {only})
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
