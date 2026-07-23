"""Tests for the 21 -> 13 model-tool consolidation (see webapp/tools.py TOOLS + dispatch()).

Covers: (1) the model-visible TOOLS list is exactly the 13 merged tools, (2) the 10 retired
model-tool names (consumers/producers/repo_routes/trace/usecase_route/use_cases_for_topic/
show_impact/show_coverage/call_graph/list_source_systems) still route in dispatch() for
backward-compat (CLI/MCP/existing tests), and (3)-(6) the new merged tools (message_flow,
usecase_routing, impact(inline=true), list_repos(inline=true)) dispatch to the right underlying
call for each argument combination.
"""
import unittest
from unittest import mock

from webapp import tools


EXPECTED_MODEL_TOOLS = {
    "impact", "hubs", "message_flow", "usecase_routing", "list_repos", "search_code",
    "read_file", "unified_impact", "show_arch", "source_system_impact", "usecase_impact",
    "search_usecases", "usecase_quality_findings",
}


class ModelToolSurfaceTests(unittest.TestCase):
    """Guard against accidental regrowth (an old name sneaking back in) or removal (a merged tool
    silently dropped) of the model-visible TOOLS list."""

    def test_tools_list_is_exactly_the_13_merged_tools(self):
        names = {entry["function"]["name"] for entry in tools.TOOLS}
        self.assertEqual(names, EXPECTED_MODEL_TOOLS)
        self.assertEqual(len(tools.TOOLS), 13, "TOOLS should have no duplicate names")


class LegacyToolBackwardCompatTests(unittest.TestCase):
    """The 10 pre-consolidation names are no longer advertised to the model (removed from TOOLS)
    but dispatch() must keep routing them for CLI/MCP callers and existing tests that still use the
    old names directly."""

    def _assert_routed(self, name, result):
        self.assertNotEqual(result, {"error": f"unknown tool: {name}"})

    def test_consumers_still_routes(self):
        with mock.patch.object(tools.msg, "who_consumes", return_value=["repoA"]) as fn:
            result = tools.dispatch("consumers", {"destination": "topic-x"})
        fn.assert_called_once_with("topic-x")
        self._assert_routed("consumers", result)

    def test_producers_still_routes(self):
        with mock.patch.object(tools.msg, "who_produces", return_value=["repoB"]) as fn:
            result = tools.dispatch("producers", {"destination": "topic-x"})
        fn.assert_called_once_with("topic-x")
        self._assert_routed("producers", result)

    def test_repo_routes_still_routes(self):
        with mock.patch.object(tools.msg, "routes_for_repo", return_value=["route1"]) as fn:
            result = tools.dispatch("repo_routes", {"repo": "repoA"})
        fn.assert_called_once_with("repoA")
        self._assert_routed("repo_routes", result)

    def test_trace_still_routes(self):
        with mock.patch.object(tools.flow, "trace", return_value={"trace": "ok"}) as fn:
            result = tools.dispatch("trace", {"use_case_id": "M2050"})
        fn.assert_called_once_with("M2050", None)
        self._assert_routed("trace", result)

    def test_usecase_route_still_routes(self):
        with mock.patch.object(tools.msg, "usecase_route", return_value={"topic": "t"}) as fn:
            result = tools.dispatch("usecase_route", {"use_case_id": "M2050"})
        fn.assert_called_once_with("M2050", None)
        self._assert_routed("usecase_route", result)

    def test_use_cases_for_topic_still_routes(self):
        with mock.patch.object(tools.msg, "reverse_lookup_use_cases", return_value={"total": 0}) as fn:
            result = tools.dispatch("use_cases_for_topic", {"topic": "marketing-batch"})
        fn.assert_called_once_with("marketing-batch", True, 50)
        self._assert_routed("use_cases_for_topic", result)

    def test_show_impact_still_routes(self):
        with mock.patch.object(tools.graph, "known_repos", return_value={"core"}), \
             mock.patch.object(tools.graph, "impact",
                                return_value={"depended_on_by": [], "depends_on": []}):
            result = tools.dispatch("show_impact", {"repo": "core"})
        self._assert_routed("show_impact", result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["view"], "impact")

    def test_show_coverage_still_routes(self):
        result = tools.dispatch("show_coverage", {"kind": "channel", "value": "sms"})
        self._assert_routed("show_coverage", result)
        self.assertEqual(result["view"], "coverage")

    def test_call_graph_still_routes(self):
        with mock.patch.object(tools.unified_impact, "call_graph",
                                return_value={"available": False}) as fn:
            result = tools.dispatch("call_graph", {"query": "IngressService"})
        fn.assert_called_once_with("IngressService")
        self._assert_routed("call_graph", result)

    def test_list_source_systems_still_routes(self):
        with mock.patch.object(tools.usecase_master, "source_systems",
                                return_value=[{"source_system": "PEGA"}]):
            result = tools.dispatch("list_source_systems", {})
        self._assert_routed("list_source_systems", result)
        self.assertEqual(result, {"items": [{"source_system": "PEGA"}]})


class MessageFlowRoutingTests(unittest.TestCase):
    """message_flow merges consumers/producers/repo_routes/trace behind one entry point,
    disambiguated by which arg is supplied (use_case_id > repo > destination)."""

    def test_direction_consume_calls_who_consumes(self):
        with mock.patch.object(tools.msg, "who_consumes", return_value=["repoA"]) as fn:
            result = tools.dispatch("message_flow",
                                     {"destination": "topic-x", "direction": "consume"})
        fn.assert_called_once_with("topic-x")
        self.assertEqual(result, {"direction": "consume", "matches": ["repoA"]})

    def test_direction_defaults_to_consume(self):
        with mock.patch.object(tools.msg, "who_consumes", return_value=["repoA"]) as fn:
            result = tools.dispatch("message_flow", {"destination": "topic-x"})
        fn.assert_called_once_with("topic-x")
        self.assertEqual(result["direction"], "consume")

    def test_direction_produce_calls_who_produces(self):
        with mock.patch.object(tools.msg, "who_produces", return_value=["repoB"]) as fn:
            result = tools.dispatch("message_flow",
                                     {"destination": "topic-x", "direction": "produce"})
        fn.assert_called_once_with("topic-x")
        self.assertEqual(result, {"direction": "produce", "matches": ["repoB"]})

    def test_direction_both_returns_both_keys(self):
        with mock.patch.object(tools.msg, "who_consumes", return_value=["repoA"]) as consume_fn, \
             mock.patch.object(tools.msg, "who_produces", return_value=["repoB"]) as produce_fn:
            result = tools.dispatch("message_flow",
                                     {"destination": "topic-x", "direction": "both"})
        consume_fn.assert_called_once_with("topic-x")
        produce_fn.assert_called_once_with("topic-x")
        self.assertEqual(result,
                          {"direction": "both", "consumers": ["repoA"], "producers": ["repoB"]})

    def test_repo_calls_routes_for_repo(self):
        with mock.patch.object(tools.msg, "routes_for_repo", return_value=["route1"]) as fn:
            result = tools.dispatch("message_flow", {"repo": "repoA"})
        fn.assert_called_once_with("repoA")
        self.assertEqual(result, ["route1"])

    def test_use_case_id_calls_flow_trace(self):
        with mock.patch.object(tools.flow, "trace", return_value={"trace": "ok"}) as fn:
            result = tools.dispatch("message_flow",
                                     {"use_case_id": "M2050", "destination": "topic-x"})
        fn.assert_called_once_with("M2050", "topic-x")
        self.assertEqual(result, {"trace": "ok"})

    def test_empty_args_returns_needs_one_of_error(self):
        result = tools.dispatch("message_flow", {})
        self.assertIn("error", result)
        self.assertIn("needs one of", result["error"])


class UsecaseRoutingTests(unittest.TestCase):
    """usecase_routing merges usecase_route (forward) + use_cases_for_topic (reverse=true)."""

    def test_forward_calls_usecase_route(self):
        with mock.patch.object(tools.msg, "usecase_route", return_value={"topic": "t"}) as fn:
            result = tools.dispatch("usecase_routing", {"use_case_id": "M2050", "topic": "t"})
        fn.assert_called_once_with("M2050", "t")
        self.assertEqual(result, {"topic": "t"})

    def test_reverse_with_topic_calls_reverse_lookup(self):
        with mock.patch.object(tools.msg, "reverse_lookup_use_cases",
                                return_value={"total": 1}) as fn:
            result = tools.dispatch("usecase_routing", {"reverse": True, "topic": "marketing-batch"})
        fn.assert_called_once_with("marketing-batch", True, 50)
        self.assertEqual(result, {"total": 1})

    def test_reverse_honors_exact_and_limit(self):
        with mock.patch.object(tools.msg, "reverse_lookup_use_cases", return_value={}) as fn:
            tools.dispatch("usecase_routing",
                            {"reverse": True, "topic": "t", "exact": False, "limit": 5})
        fn.assert_called_once_with("t", False, 5)

    def test_reverse_without_topic_is_error(self):
        result = tools.dispatch("usecase_routing", {"reverse": True})
        self.assertIn("error", result)


class ImpactInlineTests(unittest.TestCase):
    """impact(inline=true) replaces show_impact; without inline it stays the plain graph.impact
    result (mirrors tests/test_show_impact.py's direction checks)."""

    def test_inline_true_returns_view_impact(self):
        dep = {"depended_on_by": ["a", "b"], "depends_on": ["x"]}
        with mock.patch.object(tools.graph, "known_repos", return_value={"core"}), \
             mock.patch.object(tools.graph, "impact", return_value=dep):
            result = tools.dispatch("impact", {"repo": "core", "inline": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["view"], "impact")

    def test_without_inline_returns_plain_graph_impact_result(self):
        dep = {"depended_on_by": ["a"], "depends_on": []}
        with mock.patch.object(tools.graph, "impact", return_value=dep) as fn:
            result = tools.dispatch("impact", {"repo": "core"})
        fn.assert_called_once_with("core", False)
        self.assertEqual(result, dep)
        self.assertNotIn("view", result)


class ListReposInlineTests(unittest.TestCase):
    """list_repos(inline=true) replaces show_coverage. _coverage_view is pure — no data files
    needed to exercise it."""

    def test_inline_true_with_query_returns_view_coverage(self):
        result = tools.dispatch("list_repos", {"inline": True, "query": "mdc"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["view"], "coverage")

    def test_inline_true_with_channel_filters_by_channel(self):
        result = tools.dispatch("list_repos", {"inline": True, "channel": "sms"})
        self.assertEqual(result["view"], "coverage")
        self.assertIn("channel=sms", result["url"])


if __name__ == "__main__":
    unittest.main()
