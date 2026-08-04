"""Telling the investigator WHERE to look, and going back for more.

Two gaps the sub-agent had:

* targets were derived ONLY from repo ids found in the alert TEXT. The caller has usually already
  run `incident_impact`, or the user simply named the service, and the largest alert family here
  ("MDC Alert - General SHP API Error") names no repo at all — so the investigation refused and
  asked a question the caller could already answer.
* nothing said the tool may be called again inside the same turn until the evidence is enough.
  That one is prompt/schema wording, checked here only where it is machine-checkable.
"""
import unittest
from unittest import mock

from retriever import incident
from webapp import incident_investigator as inv, incident_plan, tools

ALERT_NO_REPO = "MDC Alert - General SHP API Error at 2026-07-30 03:15 HKT"
TIMES = [{"text": "2026-07-30 03:15 HKT", "timezone": "Asia/Hong_Kong",
          "ambiguous": False, "normalized": "2026-07-30 03:15:00"}]
KNOWN = ["mc-hk-hase-csl-sms-deli-job", "mc-hk-hase-aurora-push-job"]


def _parsed(identified=False, repos=None, use_cases=None):
    def _fake(*_a, **_k):
        return {"identified": identified, "repos": repos or [], "use_cases": use_cases or [],
                "times": TIMES, "metric": "", "notes": [], "environment": "prod"}
    return _fake


# Pinned OPEN: these describe target RESOLUTION, not the deployment's scope, which is narrowed to
# one app in the shipped config and has its own file (tests/test_incident_scope.py).
def _open_scope():
    return {"allowed_apps": (), "allowed_sources": incident_plan.DEFAULT_LOG_SOURCES,
            "policy": incident_plan.SCOPE_MAPPING_THEN_RULES}


class TargetRepoTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(incident_plan, "logdream_scope", _open_scope)
        patch.start()
        self.addCleanup(patch.stop)

    def _plan(self, parsed, **kwargs):
        with mock.patch.object(incident, "parse_alert", parsed), \
             mock.patch.object(incident, "known_repos", lambda repos=None: list(KNOWN)):
            return incident_plan.plan(ALERT_NO_REPO, **kwargs)

    def test_an_alert_naming_no_repo_is_still_runnable_when_the_caller_names_one(self):
        """Before this, the only outcome here was a refusal."""
        blind = self._plan(_parsed())
        self.assertFalse(blind["ok"])
        self.assertTrue(any("nothing to query" in r for r in blind["refusals"]))

        told = self._plan(_parsed(), target_repos=["mc-hk-hase-csl-sms-deli-job"])
        self.assertTrue(told["ok"])
        self.assertEqual([t["repo"] for t in told["targets"]], ["mc-hk-hase-csl-sms-deli-job"])

    def test_a_caller_supplied_target_is_marked_as_such(self):
        """Provenance is the point: a nil result on a repo the CALLER named is weaker evidence."""
        out = self._plan(_parsed(), target_repos=["mc-hk-hase-csl-sms-deli-job"])
        self.assertEqual(out["targets"][0]["source"], "supplied by the caller")
        self.assertTrue(out["targets"][0]["validated"])
        self.assertIn("does not confirm the caller named the right service", out["targets_note"])

    def test_a_repo_read_from_the_alert_is_marked_differently(self):
        out = self._plan(_parsed(identified=True,
                                 repos=[{"repo": "mc-hk-hase-csl-sms-deli-job"}]))
        self.assertEqual(out["targets"][0]["source"], "named in the alert text")
        self.assertNotIn("targets_note", out)

    def test_an_invented_repo_id_is_refused_never_queried(self):
        """'Never guess a repo' applies to the caller too — a wrong id aims production reads at the
        wrong service, and its empty result would read as 'the logs are clean'."""
        out = self._plan(_parsed(), target_repos=["mc-hk-hase-totally-made-up"])
        self.assertEqual(out["targets"], [])
        self.assertFalse(out["ok"])
        joined = " ".join(out["refusals"])
        self.assertIn("not in the repo universe", joined)
        self.assertIn("mc-hk-hase-totally-made-up", joined)
        self.assertIn("wrong name, not an empty log", joined)

    def test_a_good_id_still_runs_when_a_bad_one_came_with_it(self):
        out = self._plan(_parsed(),
                         target_repos=["mc-hk-hase-totally-made-up", "mc-hk-hase-aurora-push-job"])
        self.assertEqual([t["repo"] for t in out["targets"]], ["mc-hk-hase-aurora-push-job"])
        self.assertTrue(out["ok"])
        self.assertTrue(any("not in the repo universe" in r for r in out["refusals"]))

    def test_the_caller_wins_when_our_own_repo_index_is_missing(self):
        """An absent index/repo_tags.json is OUR gap. Refusing the caller's id for it would be
        letting a missing file veto knowledge the caller actually has — accept it, mark it
        unvalidated, and say so."""
        with mock.patch.object(incident, "parse_alert", _parsed()), \
             mock.patch.object(incident, "known_repos", lambda repos=None: []):
            out = incident_plan.plan(ALERT_NO_REPO, target_repos=["mc-hk-hase-anything"])
        self.assertEqual([t["repo"] for t in out["targets"]], ["mc-hk-hase-anything"])
        self.assertFalse(out["targets"][0]["validated"])
        self.assertIn("could not be confirmed to exist", out["targets"][0]["app_note"])

    def test_caller_repos_are_deduped_against_the_alerts_own(self):
        out = self._plan(_parsed(identified=True, repos=[{"repo": "mc-hk-hase-csl-sms-deli-job"}]),
                         target_repos=["mc-hk-hase-csl-sms-deli-job", "mc-hk-hase-aurora-push-job"])
        self.assertEqual([t["repo"] for t in out["targets"]],
                         ["mc-hk-hase-csl-sms-deli-job", "mc-hk-hase-aurora-push-job"])
        self.assertEqual(out["targets"][0]["source"], "named in the alert text")
        self.assertEqual(out["targets"][1]["source"], "supplied by the caller")

    def test_blank_entries_are_ignored_rather_than_refused(self):
        out = self._plan(_parsed(), target_repos=["", "   ", "mc-hk-hase-aurora-push-job"])
        self.assertEqual([t["repo"] for t in out["targets"]], ["mc-hk-hase-aurora-push-job"])
        self.assertFalse([r for r in out["refusals"] if "not in the repo universe" in r])

    def test_derived_keywords_come_from_the_caller_supplied_repo_too(self):
        """The target is not just an app name — it is what the graph-derived keywords hang off."""
        with mock.patch.object(incident, "parse_alert", _parsed()), \
             mock.patch.object(incident, "known_repos", lambda repos=None: list(KNOWN)), \
             mock.patch.object(incident_plan, "exception_classes", lambda repo: ["SmsDeliveryException"]):
            out = incident_plan.plan(ALERT_NO_REPO, target_repos=["mc-hk-hase-csl-sms-deli-job"])
        terms = [k["term"] for k in out["keywords"]]
        self.assertIn("SmsDeliveryException", terms)
        why = [k["why"] for k in out["keywords"] if k["term"] == "SmsDeliveryException"][0]
        self.assertIn("mc-hk-hase-csl-sms-deli-job", why)


class ToolSurfaceTests(unittest.TestCase):
    def _schema(self):
        for item in tools.TOOLS:
            if item.get("function", {}).get("name") == "incident_investigate":
                return item["function"]
        raise AssertionError("incident_investigate is not in TOOLS")

    def test_repos_is_a_declared_parameter(self):
        params = self._schema()["parameters"]["properties"]
        self.assertEqual(params["repos"]["type"], "array")
        self.assertNotIn("repos", self._schema()["parameters"]["required"])

    def test_the_description_tells_the_model_it_may_call_again(self):
        """The drill-down paragraph was written entirely around a USER follow-up; nothing said the
        agent itself should go back for more when a sweep came back thin."""
        text = self._schema()["description"]
        self.assertIn("re-callable within the same turn", text.lower())
        for signal in ("queries_executed", "app_resolved", "max_queries"):
            self.assertIn(signal, text)
        self.assertIn("BLOCKING window refusal", text)

    def test_repos_reaches_the_investigator_as_target_repos(self):
        seen = {}

        def _fake(text, **kwargs):
            seen.update(kwargs)
            yield {"type": "result", "packet": {"ok": True}}

        with mock.patch.object(inv, "investigate_events", _fake):
            list(tools.dispatch_events("incident_investigate",
                                       {"alert_text": "x", "repos": ["mc-hk-hase-a"]}))
        self.assertEqual(seen["target_repos"], ["mc-hk-hase-a"])

    def test_no_repos_passes_none_rather_than_an_empty_list(self):
        seen = {}

        def _fake(text, **kwargs):
            seen.update(kwargs)
            yield {"type": "result", "packet": {"ok": True}}

        with mock.patch.object(inv, "investigate_events", _fake):
            list(tools.dispatch_events("incident_investigate", {"alert_text": "x"}))
        self.assertIsNone(seen["target_repos"])


class RepeatCallTests(unittest.TestCase):
    """The agent loop must actually let a second investigation through, and the second packet must
    not arrive silently gutted by the first one's context spend."""

    def test_two_investigations_in_one_turn_both_run_and_both_stream(self):
        from webapp import agent

        calls = []

        def _dispatch(name, args, owner=""):
            calls.append(args.get("repos"))
            yield {"type": "subagent_step", "step": "plan", "label": "sweep %d" % len(calls),
                   "detail": {}}
            yield {"type": "result", "packet": {"ok": True, "evidence": []}}

        def _call(repos):
            return {"id": "c%s" % repos, "type": "function",
                    "function": {"name": "incident_investigate",
                                 "arguments": '{"alert_text": "x", "repos": ["%s"]}' % repos}}

        replies = iter([
            {"role": "assistant", "content": None, "tool_calls": [_call("repo-a")]},
            {"role": "assistant", "content": None, "tool_calls": [_call("repo-b")]},
            {"role": "assistant", "content": "done"},
        ])

        with mock.patch.object(agent.llm, "chat_stream", lambda m, t=None: iter([("final", next(replies))])), \
             mock.patch.object(agent.llm, "stream_text", lambda m: []), \
             mock.patch.object(tools, "dispatch_events", _dispatch):
            events = list(agent.answer_events("q"))

        self.assertEqual(calls, [["repo-a"], ["repo-b"]])
        steps = [e for e in events if e.get("type") == "subagent_step"]
        self.assertEqual([s["label"] for s in steps], ["sweep 1", "sweep 2"])
        # and both survive into the store, so the reload shows the whole investigation
        done = [e for e in events if e.get("type") == "done"][0]
        self.assertEqual(len(done["subagent_steps"]), 2)

    def test_a_starved_second_packet_announces_itself_instead_of_looking_empty(self):
        """The failure this module exists to prevent: a truncated packet read as 'nothing found'."""
        from webapp import context_budget

        budget = context_budget.Budget()
        packet = {"ok": True, "evidence": [{"kind": "log", "excerpts": ["x" * 300] * 5}
                                           for _ in range(6)]}
        budget.spent["subagent"] = budget.lanes["subagent"]      # lane fully spent by call 1
        text = budget.fit_subagent_result(packet)
        self.assertIn('"_truncated": true', text)
        self.assertIn("PREVIEW, not the whole result", text)


if __name__ == "__main__":
    unittest.main()
