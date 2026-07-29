"""The MCP allow-list / naming seam.

The security-relevant assertions are the deny-list ones: an action-taking tool must stay
un-callable even when someone declares it in config, because that damage is real and irreversible.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from webapp import mcp_registry


CONFIG = {
    "servers": {
        "logdream": {"url_env": "TEST_LOGDREAM_URL", "transport": "sse", "enabled": True},
        "cloudwatch": {"url_env": "TEST_CW_URL", "transport": "streamable_http", "enabled": True},
        "portal": {"url_env": "TEST_PORTAL_URL", "transport": "?", "enabled": False},
    },
    "operations": {
        "_README": "ignored",
        "log.list_apps": {"server": "logdream", "tool": "list_logdream_apps",
                           "args": {}, "const": {}},
        "log.read": {"server": "logdream", "tool": "read_logdream_log",
                      "args": {"app": "app", "file": "filename", "max_lines": "?"},
                      "const": {"source": "hk1"}, "_note": "doc key, not data"},
        "aws.get_alarm": {"server": "cloudwatch", "tool": "get_alarm",
                           "args": {"alarm_name": "alarmName"}, "const": {}},
        "portal.sms": {"server": "portal", "tool": "query_sms_delivery_records",
                        "args": {"tracking_id": "trackingId"}, "const": {}},
        "danger.login": {"server": "portal", "tool": "open_portal_login", "args": {}, "const": {}},
        "danger.resend": {"server": "portal", "tool": "do_sms_resend",
                           "args": {"id": "id"}, "const": {}},
    },
    "never_expose": {"tools": ["open_portal_login"],
                      "patterns": ["*resend*", "*submit*", "*send*"]},
}


class _WithConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "mcp_tools.json")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(CONFIG, handle)
        self._env = mock.patch.dict(os.environ, {
            "SDLC_MCP_TOOLS": self.path,
            "TEST_LOGDREAM_URL": "http://example.invalid/sse",
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class DenyListTests(_WithConfig):
    def test_hard_denied_tool_is_refused_even_though_it_is_declared(self):
        with self.assertRaises(mcp_registry.NotAllowed) as caught:
            mcp_registry.build_call("danger.login")
        self.assertIn("never_expose", str(caught.exception))

    def test_pattern_deny_catches_an_action_tool_by_name(self):
        with self.assertRaises(mcp_registry.NotAllowed):
            mcp_registry.build_call("danger.resend")

    def test_denied_operations_are_reported_as_blocked_not_merely_unwired(self):
        states = mcp_registry.readiness()
        self.assertEqual(states["danger.login"]["state"], "blocked")
        self.assertEqual(states["danger.resend"]["state"], "blocked")

    def test_undeclared_operation_is_refused(self):
        with self.assertRaises(mcp_registry.NotAllowed) as caught:
            mcp_registry.build_call("log.whatever")
        self.assertIn("tools/list grants no access", str(caught.exception))


class WiringTests(_WithConfig):
    def test_translates_our_names_into_theirs_and_keeps_constants(self):
        server, tool, params = mcp_registry.build_call(
            "log.read", {"app": "batchLetterPostman", "file": "otx_trace.log"})
        self.assertEqual((server, tool), ("logdream", "read_logdream_log"))
        self.assertEqual(params, {"source": "hk1", "app": "batchLetterPostman",
                                  "filename": "otx_trace.log"})

    def test_passing_an_unfilled_argument_fails_closed(self):
        """`max_lines` is still "?" — passing it must refuse rather than guess a parameter name."""
        with self.assertRaises(mcp_registry.NotWired) as caught:
            mcp_registry.build_call("log.read", {"app": "a", "file": "f", "max_lines": 100})
        self.assertIn("max_lines", str(caught.exception))
        self.assertIn("tools/list", str(caught.exception))

    def test_partial_wiring_still_works_for_the_arguments_that_are_filled(self):
        """One unfilled optional must not make the whole operation unusable — otherwise the
        intranet side cannot land the config incrementally."""
        _s, _t, params = mcp_registry.build_call("log.read", {"app": "a", "file": "f"})
        self.assertEqual(params, {"source": "hk1", "app": "a", "filename": "f"})

    def test_disabled_server_is_refused(self):
        with self.assertRaises(mcp_registry.NotAllowed) as caught:
            mcp_registry.build_call("portal.sms", {"tracking_id": "x"})
        self.assertIn("not enabled", str(caught.exception))

    def test_unmapped_argument_is_refused_rather_than_passed_through(self):
        with self.assertRaises(mcp_registry.NotWired):
            mcp_registry.build_call("aws.get_alarm", {"alarm_name": "a", "region": "hk"})

    def test_none_valued_argument_is_simply_not_sent(self):
        _s, _t, params = mcp_registry.build_call("aws.get_alarm", {"alarm_name": None})
        self.assertEqual(params, {})

    def test_documentation_keys_are_never_treated_as_data(self):
        _s, _t, params = mcp_registry.build_call("log.list_apps")
        self.assertEqual(params, {})
        self.assertNotIn("_README", mcp_registry.operations())


class ReadinessTests(_WithConfig):
    def test_states_separate_ready_unwired_disabled_and_blocked(self):
        states = mcp_registry.readiness()
        self.assertEqual(states["log.list_apps"]["state"], "ready")
        self.assertEqual(states["log.read"]["state"], "partial")
        self.assertEqual(states["portal.sms"]["state"], "disabled")
        self.assertEqual(states["danger.login"]["state"], "blocked")

    def test_summary_lists_what_is_actually_callable(self):
        summary = mcp_registry.summary()
        self.assertIn("log.list_apps", summary["ready"])
        self.assertNotIn("log.read", summary["ready"])
        self.assertTrue(summary["servers"]["logdream"]["endpoint_configured"])
        self.assertFalse(summary["servers"]["cloudwatch"]["endpoint_configured"])

    def test_addresses_come_from_env_never_from_the_committed_config(self):
        self.assertEqual(mcp_registry.server_url("logdream"), "http://example.invalid/sse")
        raw = open(self.path, encoding="utf-8").read()
        self.assertNotIn("http://example.invalid", raw)

    def test_missing_config_degrades_to_nothing_callable(self):
        with mock.patch.dict(os.environ, {"SDLC_MCP_TOOLS": "/no/such/file.json"}):
            self.assertEqual(mcp_registry.operations(), {})
            with self.assertRaises(mcp_registry.NotAllowed):
                mcp_registry.build_call("log.list_apps")


class ShippedConfigTests(unittest.TestCase):
    """The committed config/mcp_tools.json must itself be coherent."""

    def test_every_server_is_disabled_until_the_box_turns_it_on(self):
        cfg = mcp_registry.load()
        for name, spec in mcp_registry.servers(cfg).items():
            self.assertFalse(spec.get("enabled"), f"{name} ships enabled — it must not")

    def test_no_declared_operation_targets_a_denied_tool(self):
        cfg = mcp_registry.load()
        blocked = [n for n, e in mcp_registry.readiness(cfg).items() if e["state"] == "blocked"]
        self.assertEqual(blocked, [], f"shipped config declares action tools: {blocked}")

    def test_every_operation_names_a_real_server(self):
        cfg = mcp_registry.load()
        known = set(mcp_registry.servers(cfg))
        for name, spec in mcp_registry.operations(cfg).items():
            if isinstance(spec, dict):
                self.assertIn(spec.get("server"), known, name)


if __name__ == "__main__":
    unittest.main()
