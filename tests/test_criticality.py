"""Multi-axis criticality (showcase 8).

The thing being defended against is a confident single ranking. `graph.hubs()` sorts by Maven
dependents and would happily produce a "top 10 core components" that silently means "top 10
build-time dependency hubs" — so the tests assert that the axes stay separable and that the three
dimensions we cannot score are always declared.
"""
import unittest
from unittest import mock

from retriever import criticality


# api-common: everything depends on it, carries no messages.
# sms-deli-job: nothing depends on it, carries every SMS.
# Two different kinds of critical, which is the whole point.
DEPENDENTS = {
    "api-common": {"a", "b", "c", "d"},
    "api-dao": {"a", "b"},
    "sms-deli-job": set(),
}
EDGES = {
    "sms-deli-job": [{"destination": "t.sms.send", "producer_repo": "x",
                       "consumer_repo": "sms-deli-job"},
                      {"destination": "t.sms.status", "producer_repo": "sms-deli-job",
                       "consumer_repo": "y"}],
    "api-common": [],
    "api-dao": [{"destination": "t.sms.send", "producer_repo": "api-dao", "consumer_repo": "z"}],
}
USE_CASES = {
    "t.sms.send": {"items": [{"use_case": "M2101"}, {"use_case": "M9114"}]},
    "t.sms.status": {"items": [{"use_case": "M2101"}]},
}


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            mock.patch.object(criticality.graph, "load_dependency_graph",
                              lambda *a, **k: ({}, DEPENDENTS)),
            mock.patch.object(criticality.graph, "known_repos", lambda: set(EDGES)),
            mock.patch.object(criticality.messages, "routes_for_repo",
                              lambda repo: EDGES.get(repo, [])),
            mock.patch.object(criticality.messages, "reverse_lookup_use_cases",
                              lambda topic, **k: USE_CASES.get(topic, {})),
            mock.patch.object(criticality.repo_tags, "channels_for_repo", lambda repo, **k: []),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()


class AxisTests(_Fixture):
    def test_a_dependency_hub_and_a_traffic_hub_score_on_different_axes(self):
        """The failure this model exists to prevent: one list that hides which definition it used."""
        out = criticality.rank()
        by_repo = {row["repo"]: row for row in out["top"]}
        common = by_repo["api-common"]["axes"]
        deli = by_repo["sms-deli-job"]["axes"]
        self.assertEqual(common["dependency"]["rank"], 1)      # top on dependency
        self.assertEqual(common["message"]["value"], 0)        # bottom on messages
        self.assertEqual(deli["dependency"]["value"], 0)       # bottom on dependency
        self.assertEqual(deli["message"]["rank"], 1)           # top on messages

    def test_business_impact_counts_distinct_use_cases_not_topics(self):
        """Two topics that share a use case must not count it twice."""
        out = criticality.rank()
        by_repo = {row["repo"]: row for row in out["top"]}
        # sms-deli-job touches t.sms.send (M2101, M9114) and t.sms.status (M2101) => 2 distinct.
        self.assertEqual(by_repo["sms-deli-job"]["axes"]["business"]["value"], 2)

    def test_axes_are_normalised_before_being_combined(self):
        """Raw dependent counts and raw topic counts are different units; adding them would let the
        axis with bigger numbers dominate."""
        out = criticality.rank()
        for row in out["top"]:
            for axis in row["axes"].values():
                self.assertLessEqual(axis["normalised"], 1.0)
                self.assertGreaterEqual(axis["normalised"], 0.0)

    def test_ties_share_the_better_rank_rather_than_being_broken_arbitrarily(self):
        scores = {"a": 5, "b": 5, "c": 1}
        self.assertEqual(criticality._ranked(scores), {"a": 1, "b": 1, "c": 3})


class HonestyTests(_Fixture):
    def test_the_three_unscored_dimensions_are_always_declared(self):
        out = criticality.rank()
        names = {item["dimension"] for item in out["missing_dimensions"]}
        self.assertEqual(names, {"production_traffic", "incident_history", "test_coverage"})

    def test_each_missing_dimension_says_who_it_is_blocked_on(self):
        """A gap without an owner is a gap nobody will close."""
        for item in criticality.rank()["missing_dimensions"]:
            self.assertTrue(item["blocked_on"])
            self.assertTrue(item["what_would_fix_it"])
            self.assertTrue(item["why_missing"])

    def test_the_result_says_the_per_axis_ranks_are_the_real_output(self):
        out = criticality.rank()
        self.assertIn("axes rather than quoting the combined rank", out["how_to_read"])

    def test_a_zero_business_score_is_explained_as_snapshot_coverage(self):
        joined = " ".join(criticality.rank()["caveats"])
        self.assertIn("never 'no business", joined)
        self.assertIn("dev/SCT", joined)

    def test_top_is_honoured_and_ranks_are_assigned(self):
        out = criticality.rank(top=2)
        self.assertEqual(len(out["top"]), 2)
        self.assertEqual([row["overall_rank"] for row in out["top"]], [1, 2])
        self.assertEqual(out["scored_repos"], 3)


class DegradationTests(unittest.TestCase):
    def test_with_no_index_at_all_it_refuses_rather_than_ranking_nothing(self):
        def _boom(*_a, **_k):
            raise OSError("index missing")
        with mock.patch.object(criticality.graph, "load_dependency_graph", _boom), \
             mock.patch.object(criticality.graph, "known_repos", _boom):
            out = criticality.rank()
        self.assertFalse(out["ok"])
        self.assertIn("no scoring axis is available", out["error"])
        # Even a refusal states what is structurally missing, so the answer is actionable.
        self.assertTrue(out["missing_dimensions"])

    def test_one_missing_axis_still_scores_the_others_and_reports_the_error(self):
        def _boom(*_a, **_k):
            raise OSError("dependency index missing")
        with mock.patch.object(criticality.graph, "load_dependency_graph", _boom), \
             mock.patch.object(criticality.graph, "known_repos", lambda: set(EDGES)), \
             mock.patch.object(criticality.messages, "routes_for_repo",
                               lambda repo: EDGES.get(repo, [])), \
             mock.patch.object(criticality.messages, "reverse_lookup_use_cases",
                               lambda topic, **k: USE_CASES.get(topic, {})), \
             mock.patch.object(criticality.repo_tags, "channels_for_repo", lambda repo, **k: []):
            out = criticality.rank()
        self.assertTrue(out["ok"])
        self.assertNotIn("dependency", out["axes_scored"])
        self.assertIn("dependency", out["axis_errors"])
        self.assertIn("message", out["axes_scored"])


class ToolTests(_Fixture):
    def test_the_tool_is_registered_and_dispatches(self):
        from webapp import tools
        self.assertIn("critical_repos", {t["function"]["name"] for t in tools.TOOLS})
        out = tools.dispatch("critical_repos", {"top": 2})
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["top"]), 2)


if __name__ == "__main__":
    unittest.main()
