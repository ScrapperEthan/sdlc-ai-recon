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
        self._flag = mock.patch.object(config, "MCP_ENABLED", True)
        self._flag.start()
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
        for patcher in (self._search, self._parse, self._mcp, self._flag):
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
        """Owner 2026-07-29: hk1 and hkp3 are both production and hold different logs."""
        inv.investigate(ALERT)
        sources = {args.get("source") for op, args in self.calls if op == "log.read"}
        self.assertEqual(sources, {"hk1", "hkp3"})

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
            self.assertIn(entry["source"], inv.PRODUCTION_SOURCES)
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
