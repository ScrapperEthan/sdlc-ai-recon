"""A shared library answering "376 downstream" is correct and useless.

Observed 2026-08-05 on the real estate: `mc-hk-hase-api-common` reports 376 downstream repos out of
~460. That number does not describe a change's blast radius — it describes the fact that the repo is
shared. And it is unusable in exactly the case people ask about most, because shared libraries and
parents are what a change-notification question is usually about.

These tests pin the shape change: past a threshold the answer stops being a list and starts being
(a) the direct dependents, which is the notification list, (b) the channel spread, which is what
could stop working, and (c) which affected repos are independently critical. Nothing is dropped from
the report — only what the narrative leads with changes.
"""
import unittest
from unittest import mock

from retriever import blast_radius


def _items(direct, transitive):
    return ([{"repo": name, "relation": "direct"} for name in direct]
            + [{"repo": name, "relation": "transitive"} for name in transitive])


class ShapeVerdictTest(unittest.TestCase):
    def setUp(self):
        # No repo_tags and no criticality: the verdict must not depend on either being present.
        self._patches = [
            mock.patch.object(blast_radius.repo_tags, "for_repo", return_value={}),
            mock.patch.object(blast_radius.criticality, "rank", return_value={"ok": False}),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _summary(self, direct, transitive, estate):
        with mock.patch.object(blast_radius.graph, "known_repos",
                               return_value={f"r{i}" for i in range(estate)}):
            return blast_radius.summarise("lib", _items(direct, transitive), tags={})

    def test_a_shared_library_is_called_shared_infrastructure(self):
        summary = self._summary([f"d{i}" for i in range(12)],
                                [f"t{i}" for i in range(364)], estate=460)
        self.assertTrue(summary["is_hub"])
        self.assertEqual(summary["total"], 376)
        self.assertIn("SHARED INFRASTRUCTURE", summary["reading"])
        # The sentence has to say what the number is NOT, because a big count reads as big impact.
        self.assertIn("not a notification list", summary["reading"])

    def test_a_leaf_service_keeps_the_plain_list_answer(self):
        summary = self._summary(["a", "b"], ["c"], estate=460)
        self.assertFalse(summary["is_hub"])
        self.assertIn("the list itself is the answer", summary["reading"])

    def test_a_big_share_of_a_tiny_estate_is_not_a_hub(self):
        # 3 of 5 repos is 60%, but three names are not a list worth collapsing. Share alone would
        # have flipped the shape here and made a two-line answer harder to read.
        summary = self._summary(["a"], ["b", "c"], estate=5)
        self.assertFalse(summary["is_hub"])

    def test_nothing_downstream_says_what_the_number_does_not_cover(self):
        summary = self._summary([], [], estate=460)
        self.assertEqual(summary["total"], 0)
        self.assertIn("BUILD-TIME", summary["reading"])   # runtime coupling is invisible here
        self.assertIn("topic", summary["reading"])

    def test_direct_dependents_are_always_listed_in_full(self):
        summary = self._summary(["b", "a"], [f"t{i}" for i in range(400)], estate=460)
        self.assertEqual(summary["direct"], ["a", "b"])       # sorted, complete
        self.assertEqual(summary["transitive_count"], 400)

    def test_the_full_list_is_never_dropped_from_the_report(self):
        # The summary is additive. A caller that wants all 376 still has report["downstream"].
        items = _items(["a"], [f"t{i}" for i in range(400)])
        with mock.patch.object(blast_radius.graph, "known_repos",
                               return_value={f"r{i}" for i in range(460)}):
            summary = blast_radius.summarise("lib", items, tags={})
        self.assertEqual(summary["total"], len(items))


class ChannelSpreadTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(blast_radius.criticality, "rank", return_value={"ok": False})
        patch.start()
        self.addCleanup(patch.stop)

    def _summary(self, tag_by_repo, estate=460):
        def for_repo(name, _tags=None):
            return tag_by_repo.get(name, {})
        with mock.patch.object(blast_radius.repo_tags, "for_repo", side_effect=for_repo), \
             mock.patch.object(blast_radius.graph, "known_repos",
                               return_value={f"r{i}" for i in range(estate)}):
            return blast_radius.summarise(
                "lib", _items(["a"], ["b", "c"]), tags={})

    def test_channels_are_counted_from_the_repos_own_tags(self):
        summary = self._summary({
            "a": {"channel": ["sms"]},
            "b": {"channel": ["sms"]},
            "c": {"channel_declared": ["email"]},
        })
        spread = {item["channel"]: item["repos"] for item in summary["channels"]}
        self.assertEqual(spread, {"sms": 2, "email": 1})
        self.assertEqual(summary["channel_unknown_repos"], 0)

    def test_untagged_repos_are_counted_and_the_spread_is_called_a_lower_bound(self):
        # repo_tags channel coverage was measured at ~35%. Reporting "1 channel affected" out of a
        # set where two thirds have no tag would be a confident undercount.
        summary = self._summary({"a": {"channel": ["sms"]}})
        self.assertEqual(summary["channel_unknown_repos"], 2)
        self.assertIn("lower bound", summary["reading"].lower())

    def test_serves_channels_is_not_used(self):
        # It is derived FROM graph blast radius, so counting it here would let a repo inherit a
        # channel from the very relationship being measured and report it back as evidence.
        summary = self._summary({"a": {"serves_channels": ["sms", "email"]}})
        self.assertEqual(summary["channels"], [])
        self.assertEqual(summary["channel_unknown_repos"], 3)


class NotableRepoTest(unittest.TestCase):
    RANKED = {"ok": True, "top": [
        {"repo": "b", "overall_rank": 2,
         "axes": {"dependency": {"rank": 1}, "message": {"rank": 9}}},
        {"repo": "zzz-not-downstream", "overall_rank": 1, "axes": {}},
    ]}

    def _summary(self, ranked):
        with mock.patch.object(blast_radius.repo_tags, "for_repo", return_value={}), \
             mock.patch.object(blast_radius.criticality, "rank", return_value=ranked), \
             mock.patch.object(blast_radius.graph, "known_repos",
                               return_value={f"r{i}" for i in range(460)}):
            return blast_radius.summarise("lib", _items(["a"], ["b"]), tags={})

    def test_only_downstream_repos_are_reported_as_notable(self):
        summary = self._summary(self.RANKED)
        self.assertEqual([entry["repo"] for entry in summary["notable"]], ["b"])

    def test_per_axis_ranks_are_carried_not_collapsed(self):
        # criticality exists precisely because a build-time hub and a runtime traffic hub are
        # different kinds of critical. Reducing them back to one number here would undo that.
        summary = self._summary(self.RANKED)
        self.assertEqual(summary["notable"][0]["axes"], {"dependency": 1, "message": 9})

    def test_unavailable_ranking_degrades_to_empty(self):
        self.assertEqual(self._summary({"ok": False})["notable"], [])

    def test_a_raising_ranker_does_not_take_down_the_report(self):
        with mock.patch.object(blast_radius.repo_tags, "for_repo", return_value={}), \
             mock.patch.object(blast_radius.criticality, "rank", side_effect=RuntimeError("boom")), \
             mock.patch.object(blast_radius.graph, "known_repos", return_value=set()):
            summary = blast_radius.summarise("lib", _items(["a"], []), tags={})
        self.assertEqual(summary["notable"], [])
        self.assertEqual(summary["total"], 1)


class RenderTest(unittest.TestCase):
    def _render(self, direct, transitive, estate=460, tags=None, ranked=None):
        def for_repo(name, _tags=None):
            return (tags or {}).get(name, {})
        with mock.patch.object(blast_radius.repo_tags, "for_repo", side_effect=for_repo), \
             mock.patch.object(blast_radius.criticality, "rank",
                               return_value=ranked or {"ok": False}), \
             mock.patch.object(blast_radius.graph, "known_repos",
                               return_value={f"r{i}" for i in range(estate)}):
            summary = blast_radius.summarise("lib", _items(direct, transitive), tags={})
        return "\n".join(blast_radius.render_markdown(summary)), summary

    def test_hub_render_lists_direct_and_collapses_the_tail(self):
        text, _ = self._render(["team-a", "team-b"], [f"t{i}" for i in range(400)])
        self.assertIn("direct dependents (2)", text)
        self.assertIn("team-a", text)
        self.assertIn("notification/review list", text)
        self.assertIn("400", text)          # the tail is a count
        self.assertNotIn("t399", text)      # ...not an enumeration

    def test_non_hub_render_stays_compact(self):
        text, _ = self._render(["a"], ["b"])
        self.assertNotIn("notification/review list", text)

    def test_render_survives_an_empty_summary(self):
        self.assertEqual(blast_radius.render_markdown(None), ["- not computed"])

    def test_report_leads_with_the_verdict_before_the_list(self):
        import impact_report
        report = {
            "target": {"input": "lib", "kind": "repo", "value": "lib", "description": "lib",
                       "channels": [], "citations": []},
            "upstream": [], "downstream": _items(["a"], [f"t{i}" for i in range(400)]),
            "async_routes": [], "channel_chain": [], "risk_callouts": [],
            "blast_radius": {"repo": "lib", "total": 401, "direct": ["a"], "direct_count": 1,
                             "transitive_count": 400, "estate": 460, "share_of_estate": 0.87,
                             "is_hub": True, "channels": [], "channel_unknown_repos": 0,
                             "notable": [], "reading": "lib is SHARED INFRASTRUCTURE: ..."},
        }
        text = impact_report.render_report_markdown(report)
        verdict = text.index("SHARED INFRASTRUCTURE")
        listing = text.index("t399")
        self.assertLess(verdict, listing, "the verdict must come before the full list")
        # And the list is folded away rather than deleted.
        self.assertIn("<details>", text)


if __name__ == "__main__":
    unittest.main()
