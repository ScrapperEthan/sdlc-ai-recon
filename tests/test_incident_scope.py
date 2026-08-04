"""How far LogDream may be reached at all — a gate that runs before anything is queried.

Intranet audit, 2026-08-04: today exactly one app is in scope (`portal` on `hkp3`), but the
candidate rule still let **35 unrelated repos** match a name in that source's 92-app listing. The
hole is a category error that is easy to make and hard to see: passing the live-listing check means
the app EXISTS, not that we are allowed to read it. A rule-derived guess that happens to name a real
app reads, downstream, exactly like a confirmed target.

So scope is its own gate, with two knobs, both in the intranet's config and both DEFAULTING TO OPEN
— an absent knob has to behave exactly as before it existed, or pulling this change would silently
narrow a deployment nobody meant to narrow.

The distinction the tests protect hardest: "outside the scope somebody set" and "we could not work
out this repo's app name" both produce zero candidates, and they are not the same thing. The first
is the system working; the second is a gap to close. Reporting them identically would make a
deliberate restriction look like a defect, and a defect look like a decision.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from retriever import code as rcode, incident                       # noqa: E402
from webapp import incident_investigator as inv, incident_plan, mcp_client  # noqa: E402

PORTAL_REPO = "mc-hk-hase-portal-web"
OTHER_REPO = "mc-hk-hase-csl-sms-deli-job"


def _server(**overrides):
    spec = {"url_env": "X", "transport": "sse", "enabled": True,
            "sources": {"hkl": {"query_by_default": False}, "hkp3": {"query_by_default": True}},
            "log_files": {"trace": "otx_trace.log", "exception": "exception.log", "other": []}}
    spec.update(overrides)
    return {"logdream": spec}


class _ScopeCase(unittest.TestCase):
    MAPPING = {PORTAL_REPO: "portal"}

    def _apply(self, mapping=None, **server):
        for patcher in (mock.patch.object(incident_plan.mcp_registry, "servers",
                                          lambda cfg=None: _server(**server)),
                        mock.patch.object(incident_plan, "_app_map",
                                          lambda: dict(self.MAPPING if mapping is None
                                                       else mapping))):
            patcher.start()
            self.addCleanup(patcher.stop)


class DefaultsAreOpenTests(_ScopeCase):
    """Neither knob set: behave exactly as before they existed."""

    def test_no_knobs_means_the_naming_rule_still_runs(self):
        self._apply(mapping={})
        candidates = incident_plan.app_candidates(OTHER_REPO)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["confidence"], "candidate")
        self.assertEqual(incident_plan.scope_refusal(OTHER_REPO), "")

    def test_an_unrecognised_policy_value_does_not_silently_narrow(self):
        self._apply(mapping={}, app_resolution_policy="whatever_this_is")
        self.assertTrue(incident_plan.app_candidates(OTHER_REPO))


class AllowedAppsTests(_ScopeCase):
    """The allow-list, applied to whatever the mapping or the rule produced."""

    def test_a_mapped_repo_inside_the_list_resolves(self):
        self._apply(allowed_apps=["portal"], app_resolution_policy="explicit_mapping_only")
        candidates = incident_plan.app_candidates(PORTAL_REPO)
        self.assertEqual([entry["app"] for entry in candidates], ["portal"])
        self.assertEqual(candidates[0]["confidence"], "confirmed")

    def test_a_mapped_repo_outside_the_list_is_refused_with_the_reason(self):
        self._apply(mapping={OTHER_REPO: "cslSmsDeli"}, allowed_apps=["portal"])
        self.assertEqual(incident_plan.app_candidates(OTHER_REPO), [])
        refusal = incident_plan.scope_refusal(OTHER_REPO)
        self.assertIn("OUTSIDE the configured scope", refusal)
        self.assertIn("portal", refusal)
        self.assertIn("NOT an empty log", refusal)

    def test_the_rule_cannot_smuggle_a_name_past_the_allow_list(self):
        """The audit's actual finding: the rule produces a plausible name, the name exists in the
        server's 92-app listing, and downstream that is indistinguishable from a confirmed target."""
        self._apply(mapping={}, allowed_apps=["portal"])
        self.assertEqual(incident_plan.app_candidates(OTHER_REPO), [])

    def test_an_empty_allow_list_is_no_restriction_not_a_total_block(self):
        self._apply(mapping={}, allowed_apps=[])
        self.assertTrue(incident_plan.app_candidates(OTHER_REPO))


class ExplicitMappingOnlyTests(_ScopeCase):

    def test_an_unmapped_repo_yields_nothing_and_says_why(self):
        self._apply(app_resolution_policy="explicit_mapping_only")
        self.assertEqual(incident_plan.app_candidates(OTHER_REPO), [])
        refusal = incident_plan.scope_refusal(OTHER_REPO)
        self.assertIn("explicit_mapping_only", refusal)
        self.assertIn("logdream_app_map.json", refusal)
        # It has to say what a guessed name would have COST, or the next person removes the policy.
        self.assertIn("would read as a confirmed target", refusal)

    def test_a_mapped_repo_still_resolves_under_the_strict_policy(self):
        self._apply(app_resolution_policy="explicit_mapping_only")
        self.assertEqual([e["app"] for e in incident_plan.app_candidates(PORTAL_REPO)], ["portal"])

    def test_out_of_scope_and_unmappable_are_reported_differently(self):
        """Both are zero candidates. One is the system working, one is a gap."""
        self._apply(mapping={OTHER_REPO: "cslSmsDeli"}, allowed_apps=["portal"],
                    app_resolution_policy="explicit_mapping_only")
        out_of_scope = incident_plan.scope_refusal(OTHER_REPO)          # mapped, wrong app
        unmapped = incident_plan.scope_refusal("mc-hk-hase-never-heard-of-it")
        self.assertIn("OUTSIDE the configured scope", out_of_scope)
        self.assertIn("no entry in the intranet's LogDream app map", unmapped)
        self.assertNotEqual(out_of_scope, unmapped)


class SourceScopeTests(_ScopeCase):
    """A caller-supplied `sources` is a drill-down, not an override."""

    def setUp(self):
        super().setUp()
        self._apply()

    def test_a_disabled_source_cannot_be_reached_by_asking_for_it(self):
        """`hkl` is `query_by_default: false`. Before this, naming it in a drill-down sent it
        straight to the wire — a source the intranet deliberately switched off."""
        plan = self._plan(sources=["hkl"])
        self.assertEqual(plan["sources"], ["hkp3"])
        self.assertTrue(any("not in the configured set" in r for r in plan["refusals"]))
        self.assertTrue(any("hkl" in r for r in plan["refusals"]))

    def test_an_invented_source_is_refused_rather_than_queried(self):
        plan = self._plan(sources=["hkp9"])
        self.assertEqual(plan["sources"], ["hkp3"])
        self.assertTrue(any("hkp9" in r for r in plan["refusals"]))

    def test_a_legitimate_narrowing_still_works(self):
        plan = self._plan(sources=["hkp3"])
        self.assertEqual(plan["sources"], ["hkp3"])
        self.assertFalse(any("not in the configured set" in r for r in plan["refusals"]))

    def _plan(self, **kwargs):
        from retriever import code as rcode, incident
        with mock.patch.object(incident, "parse_alert", lambda *a, **k: {
                "identified": True, "repos": [], "use_cases": [], "metric": "", "notes": [],
                "environment": "prod", "times": []}), \
                mock.patch.object(rcode, "search_code", lambda *a, **k: []):
            return incident_plan.plan("some alert", **kwargs)


class LogFileScopeTests(_ScopeCase):

    def test_the_files_to_read_come_from_the_config(self):
        self._apply()
        self.assertEqual(incident_plan.preferred_log_files(), ("otx_trace.log", "exception.log"))

    def test_a_file_added_by_the_box_is_picked_up_without_a_code_change(self):
        self._apply(log_files={"trace": "otx_trace.log", "exception": "exception.log",
                               "other": ["sftp.log", "audit.log"]})
        self.assertEqual(incident_plan.preferred_log_files(),
                         ("otx_trace.log", "exception.log", "sftp.log", "audit.log"))

    def test_unfilled_placeholders_are_skipped_not_queried(self):
        self._apply(log_files={"trace": "?", "exception": "exception.log", "other": ["?"]})
        self.assertEqual(incident_plan.preferred_log_files(), ("exception.log",))

    def test_an_empty_config_falls_back_rather_than_planning_nothing_to_read(self):
        self._apply(log_files={})
        self.assertEqual(incident_plan.preferred_log_files(), incident_plan.DEFAULT_LOG_FILES)


class StrictZeroCallTests(_ScopeCase):
    """Out of scope must cost ZERO calls to production — not one metadata call and then a discovery.

    Intranet, 2026-08-04: the scope gate stopped `search_files` and `read` but leaked one
    `log.list_apps` in front of them, because `plan.ok` was true whenever `targets` was non-empty
    and an out-of-scope repo still produces a target (deliberately — the UI needs to show WHY it
    was refused). `list_apps` runs before any per-target check, so the leak was structural rather
    than a missing condition.

    These are their three required regressions, plus the mixed case they asked to be sure about.
    """

    def setUp(self):
        super().setUp()
        self._apply(allowed_apps=["portal"], app_resolution_policy="explicit_mapping_only")
        self.calls = []
        patchers = [
            mock.patch.object(inv.config, "MCP_ENABLED", True),
            mock.patch.object(mcp_client, "call", self._record),
            mock.patch.object(rcode, "search_code", lambda *a, **k: []),
            # Every abstract arg mapped to a real parameter name, as the box's config has them.
            # The shipped template still carries "?" placeholders, and an unwired arg is dropped —
            # which would make this test assert on the arguments of calls that never carried any.
            mock.patch.object(inv.mcp_registry, "operations", lambda cfg=None: {
                "log.list_apps": {"args": {"source": "source"}},
                "log.search_files": {"args": {"app": "app", "source": "source",
                                              "keyword": "keyword"}},
                "log.read": {"args": {"app": "app", "source": "source", "file": "file_name",
                                      "mode": "read_mode", "keyword": "keyword",
                                      "alert_time": "alert_time", "timezone": "timezone"}}}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _record(self, operation, args=None, **_kw):
        self.calls.append((operation, dict(args or {})))
        if operation == "log.list_apps":
            return {"ok": True, "text": json.dumps(
                {"entries": [{"name": "portal", "entry_type": "dir"},
                             {"name": "cslSmsDeli", "entry_type": "dir"}]})}
        if operation == "log.search_files":
            return {"ok": True, "text": json.dumps(["/apps/portal/log/exception.log"])}
        return {"ok": True, "text": json.dumps(
            {"lines": ["2026-07-30 03:15:01 ERROR CPUUtilization breach"],
             "retrieval_method": "keyword", "line_count": 1})}

    def _investigate(self, repos, use_cases=()):
        parsed = {"identified": True,
                  "repos": [{"repo": name, "confidence": "confirmed"} for name in repos],
                  "use_cases": [{"use_case": uc} for uc in use_cases],
                  "metric": "CPUUtilization", "notes": [], "environment": "prod",
                  "times": [{"text": "2026-07-30 03:15 HKT", "timezone": "Asia/Hong_Kong",
                             "ambiguous": False, "normalized": "2026-07-30 03:15:00"}]}
        with mock.patch.object(incident, "parse_alert", lambda *a, **k: parsed):
            return inv.investigate("an alert at 2026-07-30 03:15 HKT")

    def _ops(self):
        return [operation for operation, _args in self.calls]

    def test_1_an_out_of_scope_repo_with_a_valid_window_makes_no_calls_at_all(self):
        packet = self._investigate([OTHER_REPO])
        self.assertEqual(self._ops(), [], "an out-of-scope repo must not reach production")
        self.assertEqual(packet["evidence"], [])
        joined = " ".join(packet["not_investigated"])
        self.assertTrue("explicit_mapping_only" in joined or "OUTSIDE the configured scope" in joined)

    def test_2_a_use_case_alone_does_not_open_the_log_branch(self):
        """`parsed["use_cases"]` used to make the plan runnable, but nothing converts a use case
        into a repo/app target — so the branch opened with nothing to query and called `list_apps`
        to find that out."""
        packet = self._investigate([], use_cases=["M2101"])
        self.assertEqual([op for op in self._ops() if op.startswith("log.")], [])
        self.assertFalse(packet["plan"]["ok"])

    def test_3_a_mixed_input_queries_the_in_scope_target_only(self):
        packet = self._investigate([PORTAL_REPO, OTHER_REPO])
        apps = {args.get("app") for operation, args in self.calls
                if operation in ("log.search_files", "log.read")}
        self.assertEqual(apps, {"portal"})
        self.assertIn("log.list_apps", self._ops())        # legitimate now: there IS a target
        self.assertTrue(packet["evidence"])
        # The refused one still explains itself rather than vanishing.
        notes = {t["repo"]: t.get("app_note", "") for t in packet["plan"]["targets"]}
        self.assertIn(OTHER_REPO, notes)
        self.assertTrue(notes[OTHER_REPO])

    def test_the_plan_publishes_which_targets_are_runnable(self):
        packet = self._investigate([PORTAL_REPO, OTHER_REPO])
        self.assertEqual(packet["plan"]["log_targets"], [PORTAL_REPO])
        self.assertEqual([t["repo"] for t in packet["plan"]["targets"]],
                         [PORTAL_REPO, OTHER_REPO])       # both kept for the audit trail

    def test_the_investigator_does_not_trust_a_supplied_plan(self):
        """`query_plan` is a parameter — a caller can hand in a plan this module did not build.
        `ok: true` with no runnable target must still make no calls."""
        forged = {"ok": True, "any_runnable": True, "sources": ["hkp3"],
                  "log_files": ["exception.log"], "refusals": [], "keywords": [{"term": "x"}],
                  "window": {"alert_time": "2026-07-30 03:15:00", "timezone": "Asia/Hong_Kong"},
                  "targets": [{"repo": OTHER_REPO, "app_candidates": [], "app_note": "out"}],
                  "cloudwatch": {"runnable": False, "refusals": []},
                  "portal": {"runnable": False, "refusals": []}}
        list(inv.investigate_events("alert", query_plan=forged))
        self.assertEqual(self._ops(), [])


class ShippedScopeTests(unittest.TestCase):
    """What the committed config actually says, so a merge that drops it is visible."""

    def test_the_shipped_config_is_portal_only_on_hkp3(self):
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "mcp_tools.json"), encoding="utf-8-sig") as handle:
            logdream = json.load(handle)["servers"]["logdream"]
        self.assertEqual(logdream["allowed_apps"], ["portal"])
        self.assertEqual(logdream["app_resolution_policy"], "explicit_mapping_only")
        self.assertTrue(logdream["sources"]["hkp3"]["query_by_default"])
        self.assertFalse(logdream["sources"]["hkl"]["query_by_default"])
        # The spelling itself, pinned. `hk1` (digit) for `hkl` (letter L) has now been wrong twice.
        self.assertIn("hkl", logdream["sources"])
        self.assertNotIn("hk1", logdream["sources"])

    def test_the_config_explains_how_to_add_an_app_later(self):
        """The scope will be widened by someone who is not in this conversation."""
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "mcp_tools.json"), encoding="utf-8-sig") as handle:
            readme = " ".join(json.load(handle)["servers"]["logdream"]["_scope_README"])
        self.assertIn("logdream_app_map.json", readme)
        self.assertIn("allowed_apps", readme)


if __name__ == "__main__":
    unittest.main()
