"""A successful log read is not the same thing as a keyword hit.

Intranet live probe, 2026-08-04, on the recovered LogDream: asking `read_logdream_log` for a keyword
that cannot possibly match does NOT return zero rows. It returns the last N lines of the file with
`retrieval_method: tail`, byte-identical to a plain tail read. Same for `mode=auto` and for sending
no mode at all. Their response was honest about it the whole time — it carries the field that says
so — and this side simply was not reading it: any non-empty response became
"keyword X: N lines found".

That is the worst defect this feature can have. It does not fail loudly; it produces a confident,
wrong finding, attributed to a keyword nobody confirmed was present, on real production lines that
have nothing to do with the incident.

Two independent checks now stand between a response and the word "hit", and the second is the one
that decides:

* what they SAY they did (`retrieval_method`), which catches the downgrade by name;
* what the lines ACTUALLY contain, checked locally — which does not depend on their field names,
  their vocabulary, or their honesty.

The seven cases below are the intranet's required list (handoff §2.5), in their order.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retriever import code as rcode, incident                        # noqa: E402
from webapp import (config, incident_investigator as inv, incident_parse,  # noqa: E402
                    incident_plan, mcp_client)

TERM = "CPUUtilization"
# What the server hands back when the keyword did NOT match: the tail of the file, which on a busy
# service is real, recent, and completely unrelated to the search.
TAIL_LINES = ["2026-07-30 09:41:02 INFO  heartbeat ok",
              "2026-07-30 09:41:07 INFO  scheduled sweep finished",
              "2026-07-30 09:41:12 INFO  connection pool 4/16"]
MATCHING_LINES = ["2026-07-30 03:15:01 ERROR SmsDeliveryException CPUUtilization 94% breach",
                  "2026-07-30 03:15:04 ERROR TimeoutException CPUUtilization sustained"]


def _body(lines, retrieval_method=None, count=None):
    payload = {"lines": list(lines), "line_count": count if count is not None else len(lines)}
    if retrieval_method is not None:
        payload["retrieval_method"] = retrieval_method
    return {"ok": True, "text": json.dumps(payload)}


class ValidatorTests(unittest.TestCase):
    """The gate itself, in isolation. `incident_parse` owns it so the console and the investigator
    cannot drift — the console showing `retrieval_method=tail` while the product caller still
    consumed it as a hit is precisely the split the intranet warned about."""

    def test_1_a_keyword_request_answered_with_tail_yields_no_evidence(self):
        verdict = incident_parse.validate_log_read_semantics(
            _body(TAIL_LINES, "tail"), requested_mode="keyword", requested_keyword=TERM)
        self.assertEqual(verdict["outcome"], "semantic_downgrade")
        self.assertTrue(verdict["semantic_downgrade"])
        self.assertFalse(verdict["evidence_accepted"])
        self.assertEqual(verdict["literal_matches"], [])
        self.assertEqual(verdict["actual_method"], "tail")

    def test_2_lines_that_do_not_contain_the_term_are_not_a_hit(self):
        """Even with no `retrieval_method` at all. The local check is the one that decides, so a
        server that reports nothing — or reports the method we asked for while returning the wrong
        rows — still cannot manufacture a match."""
        for method in (None, "keyword", "grep", "something_new"):
            with self.subTest(method=method):
                verdict = incident_parse.validate_log_read_semantics(
                    _body(TAIL_LINES, method), requested_mode="keyword", requested_keyword=TERM)
                self.assertFalse(verdict["evidence_accepted"])
                self.assertEqual(verdict["literal_matches"], [])

    def test_3_backtrack_lines_without_the_term_are_context_not_a_hit(self):
        verdict = incident_parse.validate_log_read_semantics(
            _body(TAIL_LINES, "alert_time_backtrack"),
            requested_mode="alert_time_backtrack", requested_keyword=TERM)
        self.assertEqual(verdict["outcome"], "time_context")
        self.assertFalse(verdict["evidence_accepted"])
        self.assertEqual(len(verdict["context_lines"]), 3)

    def test_4_only_the_locally_matching_lines_survive(self):
        """A mixed response must not have its context counted into the finding. Before this, a read
        returning 2 matches and 3 unrelated lines reported `lines_seen: 5`."""
        verdict = incident_parse.validate_log_read_semantics(
            _body(MATCHING_LINES + TAIL_LINES, "keyword"),
            requested_mode="keyword", requested_keyword=TERM)
        self.assertEqual(verdict["outcome"], "keyword_match")
        self.assertTrue(verdict["evidence_accepted"])
        self.assertEqual(verdict["literal_matches"], MATCHING_LINES)

    def test_4b_a_downgrade_that_happens_to_contain_the_term_is_still_a_hit(self):
        """If the tail genuinely contains the term, it contains the term. What is refused is calling
        an UNCONFIRMED line a match — not confirmed lines that arrived by an unexpected route."""
        verdict = incident_parse.validate_log_read_semantics(
            _body(MATCHING_LINES, "tail"), requested_mode="keyword", requested_keyword=TERM)
        self.assertEqual(verdict["outcome"], "keyword_match")
        self.assertTrue(verdict["evidence_accepted"])

    def test_5_an_unreadable_body_is_a_parser_gap_not_an_empty_log(self):
        verdict = incident_parse.validate_log_read_semantics(
            {"ok": True, "text": json.dumps({"rows": ["a"]})},
            requested_mode="keyword", requested_keyword=TERM)
        self.assertEqual(verdict["outcome"], "unreadable")
        self.assertFalse(verdict["evidence_accepted"])
        self.assertTrue(verdict["error"])

    def test_a_genuinely_empty_response_is_no_match_not_a_downgrade(self):
        """The distinction the whole exercise is about: "we searched and found nothing" is a real
        answer; "we did not search" is not. They must not collapse into one another."""
        verdict = incident_parse.validate_log_read_semantics(
            _body([], "keyword"), requested_mode="keyword", requested_keyword=TERM)
        self.assertEqual(verdict["outcome"], "no_match")
        self.assertFalse(verdict["semantic_downgrade"])

    def test_the_method_vocabulary_is_configurable_like_every_other_name(self):
        """Their words, their file. Same seam the argument names, shapes and formats already use."""
        with mock.patch.object(incident_parse.mcp_registry, "operations", lambda cfg=None: {
                "log.read": {"request": {"tail_methods": ["LAST_N_LINES"]}}}):
            verdict = incident_parse.validate_log_read_semantics(
                _body(TAIL_LINES, "LAST_N_LINES"), requested_mode="keyword", requested_keyword=TERM)
        self.assertEqual(verdict["outcome"], "semantic_downgrade")

    def test_the_field_holding_the_method_is_configurable_too(self):
        with mock.patch.object(incident_parse.mcp_registry, "operations", lambda cfg=None: {
                "log.read": {"response": {"retrieval_method": "meta.how_read"}}}):
            verdict = incident_parse.validate_log_read_semantics(
                {"ok": True, "text": json.dumps(
                    {"lines": TAIL_LINES, "meta": {"how_read": "tail"}})},
                requested_mode="keyword", requested_keyword=TERM)
        self.assertEqual(verdict["actual_method"], "tail")
        self.assertEqual(verdict["outcome"], "semantic_downgrade")

    def test_an_empty_keyword_matches_nothing_rather_than_everything(self):
        self.assertEqual(incident_parse.literal_matches(TAIL_LINES, ""), [])
        self.assertEqual(incident_parse.literal_matches(TAIL_LINES, "   "), [])

    def test_matching_is_case_insensitive_but_not_fuzzy(self):
        self.assertEqual(len(incident_parse.literal_matches(MATCHING_LINES, "cpuutilization")), 2)
        self.assertEqual(incident_parse.literal_matches(MATCHING_LINES, "CPU Utilization"), [])


class InvestigationTests(unittest.TestCase):
    """End to end: what the packet says after a downgraded read."""

    ALERT = ("prodECS_mc-hk-hase-csl-sms-deli-job_service_CPUUtilizationMINOR[80percent] "
             "at 2026-07-30 03:15 HKT")

    def setUp(self):
        self._patchers = [
            mock.patch.object(config, "MCP_ENABLED", True),
            mock.patch.object(config, "INCIDENT_RAW_LOGS", False),
            mock.patch.object(incident_plan, "log_sources", lambda: ("hkl",)),
            mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.list_apps": {"args": {"source": "source"}},
                "log.search_files": {"args": {"app": "app", "source": "source",
                                              "keyword": "keyword"}},
                "log.read": {"args": {"app": "app", "source": "source", "file": "file_name",
                                      "mode": "read_mode", "keyword": "keyword",
                                      "alert_time": "alert_time", "timezone": "timezone"}}}),
            mock.patch.object(incident, "parse_alert", lambda *a, **k: {
                "identified": True,
                "repos": [{"repo": "mc-hk-hase-csl-sms-deli-job", "confidence": "confirmed"}],
                "use_cases": [], "metric": TERM, "notes": [], "environment": "prod",
                "times": [{"text": "2026-07-30 03:15 HKT", "timezone": "Asia/Hong_Kong",
                           "ambiguous": False, "normalized": "2026-07-30 03:15:00"}]}),
            mock.patch.object(rcode, "search_code", lambda *a, **k: []),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, read_reply):
        def _call(operation, args=None, **_kw):
            if operation == "log.list_apps":
                return {"ok": True, "text": json.dumps(
                    {"entries": [{"name": "cslSmsDeli", "entry_type": "dir"}]})}
            if operation == "log.search_files":
                return {"ok": True, "text": json.dumps(["/apps/cslSmsDeli/log/otx_trace.log"])}
            return read_reply
        with mock.patch.object(mcp_client, "call", _call):
            return inv.investigate(self.ALERT)

    def test_6_a_downgrade_produces_no_evidence_and_is_not_reported_as_clean(self):
        packet = self._run(_body(TAIL_LINES, "tail"))
        self.assertEqual(packet["evidence"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("DOWNGRADED", joined)
        self.assertIn("NOT 'no errors in the log'", joined)
        # The lines it refused must not leak into the packet in any form.
        blob = json.dumps(packet, ensure_ascii=False)
        for line in TAIL_LINES:
            self.assertNotIn(line, blob)

    def test_7_a_successful_call_is_recorded_as_executed_with_evidence_accepted_false(self):
        """The intranet's exact ask: the query DID run — that is a fact worth keeping — and
        separately, nothing it returned was allowed to become evidence."""
        packet = self._run(_body(TAIL_LINES, "tail"))
        self.assertTrue(packet["queries_executed"])
        for entry in packet["queries_executed"]:
            self.assertFalse(entry["evidence_accepted"])
            self.assertEqual(entry["read_outcome"], "semantic_downgrade")
            self.assertEqual(entry["retrieval_method"], "tail")

    def test_context_only_reads_say_the_keyword_was_never_confirmed(self):
        packet = self._run(_body(TAIL_LINES, "alert_time_backtrack"))
        self.assertEqual(packet["evidence"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertIn("Context is not a hit", joined)
        for entry in packet["queries_executed"]:
            self.assertEqual(entry["read_outcome"], "time_context")

    def test_a_real_match_still_becomes_evidence(self):
        """The gate must not have turned the feature off. A confirmed hit is still a hit."""
        packet = self._run(_body(MATCHING_LINES, "keyword"))
        self.assertTrue(packet["evidence"])
        self.assertEqual(packet["evidence"][0]["lines_seen"], 2)
        self.assertTrue(packet["queries_executed"][0]["evidence_accepted"])

    def test_a_mixed_response_counts_only_the_confirmed_lines(self):
        packet = self._run(_body(MATCHING_LINES + TAIL_LINES, "keyword"))
        self.assertEqual(packet["evidence"][0]["lines_seen"], 2)      # not 5
        blob = json.dumps(packet, ensure_ascii=False)
        for line in TAIL_LINES:
            self.assertNotIn(line, blob)

    def test_a_tool_error_body_still_never_becomes_evidence(self):
        """The older gate, re-asserted here because the new one runs after it and must not have
        moved the goalposts: a tool that ran and complained is not a log finding."""
        packet = self._run({"ok": False, "tool_reported_error": True,
                            "text": "unknown source hkl"})
        self.assertEqual(packet["evidence"], [])
        self.assertIn("REPORTED AN ERROR", " ".join(packet["not_investigated"]))


if __name__ == "__main__":
    unittest.main()
