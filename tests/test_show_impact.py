import unittest
from unittest import mock

from webapp import tools


class ShowImpactDirectionTests(unittest.TestCase):
    """`show_impact` answers "改 X 会连累谁". The blast radius = repos that DEPEND ON X and break if
    it changes (graph.impact's `depended_on_by`) = downstream. `depends_on` is what X itself needs =
    upstream. These labels were inverted once (下游 10 / 上游 345 for a core lib); lock the direction."""

    def _dispatch(self, dep):
        with mock.patch.object(tools.graph, "known_repos", return_value={"core"}), \
             mock.patch.object(tools.graph, "impact", return_value=dep):
            return tools.dispatch("show_impact", {"repo": "core"})

    def test_downstream_is_depended_on_by(self):
        # a core lib: many consumers depend on it, it needs few — mirrors tracking-core (345 vs 10).
        dep = {"depended_on_by": ["a", "b", "c"], "depends_on": ["x"]}
        result = self._dispatch(dep)
        self.assertTrue(result["ok"])
        rel = result["impact"]["repos"]["by_relation"]
        self.assertEqual(rel["dependency-downstream"], 3, "downstream must be the affected consumers")
        self.assertEqual(rel["dependency-upstream"], 1, "upstream must be this repo's own deps")
        self.assertIn("下游（受影响）3", result["summary"])
        self.assertIn("上游（依赖）1", result["summary"])
        # the sample chips are the affected consumers (the answer to "连累谁"), not the dependencies
        self.assertEqual(result["impact"]["repos"]["sample"], ["a", "b", "c"])

    def test_unknown_repo_rejected(self):
        with mock.patch.object(tools.graph, "known_repos", return_value={"core"}):
            result = tools.dispatch("show_impact", {"repo": "nope"})
        self.assertFalse(result["ok"])

    def test_missing_index_is_diagram_only(self):
        # index unavailable -> the diagram still renders; return ok with just url+summary, never crash.
        with mock.patch.object(tools.graph, "known_repos", side_effect=RuntimeError("no index")):
            result = tools.dispatch("show_impact", {"repo": "core"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["view"], "impact")
        self.assertIn("url", result)
        self.assertNotIn("impact", result)


if __name__ == "__main__":
    unittest.main()
