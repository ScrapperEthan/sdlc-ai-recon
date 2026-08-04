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
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp import incident_plan  # noqa: E402

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
