"""The browser-facing MCP console: what it shows, what it refuses, and what it never lets out.

The console is a SECOND path from a production system to a browser, so the tests that matter here
are the negative ones. Three properties, in descending order of how much damage a regression does:

1. Nothing reaches production that the allow-list and deny baseline would not already allow — the
   console takes an abstract operation name and has no parameter that can name a tool.
2. Nothing leaves the process unredacted, regardless of which operation was called. `data_class` is
   our guess about their data; a test that only checked redaction on "payload" operations would let
   a wrong guess become an exposure.
3. Nothing in a result or an error names an endpoint. Addresses are kept out of git precisely so
   they stay out of screenshots too.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp import config, mcp_client, mcp_console, mcp_registry  # noqa: E402


CONFIG = {
    "servers": {
        "logdream": {"url_env": "TEST_LOGDREAM_URL", "transport": "sse", "enabled": True,
                     "_note": ["two lines", "of note"]},
        "portal": {"url_env": "TEST_PORTAL_URL", "transport": "?", "enabled": False},
    },
    "operations": {
        "log.list_apps": {"server": "logdream", "tool": "list_logdream_apps",
                          "args": {}, "const": {}},
        "log.read": {"server": "logdream", "tool": "read_logdream_log",
                     "args": {"app": "app", "file": "file_name", "keyword": "?"},
                     "const": {"format": "json"}},
        "portal.sms_by_tracking_id": {"server": "portal", "tool": "query_sms_by_id",
                                      "args": {"tracking_id": "trackingId"}},
        # A denied tool someone has nonetheless declared: the registry must refuse it regardless.
        "portal.resend": {"server": "portal", "tool": "do_resend_sms", "args": {}},
    },
    "never_expose": {"tools": [], "patterns": []},
}


def _tool_result(text, structured=None, is_error=False):
    """A `tools/call` result the way a real server frames one."""
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


class _FakeSession:
    """Stands in for a live MCP session so nothing opens a socket."""

    protocol = "2024-11-05"
    truncated = False
    server_info = {"name": "fake", "version": "1"}

    def __init__(self, result):
        self._result = result
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def request(self, method, params=None):
        self.calls.append((method, params))
        return self._result


class ConsoleTestCase(unittest.TestCase):

    def setUp(self):
        self._patches = [
            mock.patch.object(mcp_registry, "load", lambda: json.loads(json.dumps(CONFIG))),
            mock.patch.dict(os.environ, {"TEST_LOGDREAM_URL": "http://logs.internal:8092/sse"}),
            mock.patch.object(config, "MCP_ENABLED", True),
            mock.patch.object(config, "MCP_CONSOLE", True),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, operation, args=None, result=None, is_error=False):
        session = _FakeSession(result if result is not None else _tool_result("ok"))
        with mock.patch.object(mcp_client, "_session", lambda *a, **k: session):
            return mcp_console.run(operation, args or {}), session


class CatalogTests(ConsoleTestCase):

    def test_catalog_lists_servers_and_operations_without_calling(self):
        with mock.patch.object(mcp_client, "_session", side_effect=AssertionError("no sockets")):
            catalog = mcp_console.catalog()
        self.assertIn("logdream", catalog["servers"])
        self.assertIn("log.read", catalog["operations"])
        self.assertEqual(catalog["servers"]["logdream"]["transport"], "sse")
        self.assertTrue(catalog["servers"]["logdream"]["endpoint_configured"])

    def test_catalog_never_carries_an_endpoint(self):
        """The addresses live in env vars so they stay out of git; a panel that printed them would
        put them straight back into a screenshot."""
        blob = json.dumps(mcp_console.catalog(), ensure_ascii=False)
        self.assertNotIn("logs.internal", blob)
        self.assertNotIn("8092", blob)
        # But whether one is configured, and which variable holds it, are exactly what an operator
        # needs to diagnose "why is this disabled".
        self.assertIn("TEST_LOGDREAM_URL", blob)

    def test_catalog_reports_argument_wiring_per_field(self):
        args = {arg["name"]: arg for arg in mcp_console.catalog()["operations"]["log.read"]["args"]}
        self.assertTrue(args["app"]["wired"])
        self.assertEqual(args["app"]["their_name"], "app")
        self.assertFalse(args["keyword"]["wired"])
        self.assertEqual(args["keyword"]["their_name"], "")

    def test_catalog_states_match_registry_readiness(self):
        catalog = mcp_console.catalog()
        self.assertEqual(catalog["operations"]["log.list_apps"]["state"], "ready")
        self.assertEqual(catalog["operations"]["log.read"]["state"], "partial")
        # portal is enabled:false in the config, so its operations are disabled, not unwired.
        self.assertEqual(catalog["operations"]["portal.sms_by_tracking_id"]["state"], "disabled")
        self.assertEqual(catalog["operations"]["portal.resend"]["state"], "blocked")

    def test_catalog_renders_a_list_valued_note_as_text(self):
        self.assertEqual(mcp_console.catalog()["servers"]["logdream"]["note"], "two lines\nof note")

    def test_catalog_works_with_calling_disabled(self):
        with mock.patch.object(config, "MCP_ENABLED", False):
            catalog = mcp_console.catalog()
        self.assertFalse(catalog["calling_enabled"])
        self.assertIn("log.read", catalog["operations"])
        self.assertIn("SDLC_MCP_ENABLED", catalog["calling_note"])

    def test_every_operation_carries_a_purpose(self):
        """A row with no sentence under it is a row nobody can act on. The built-in purposes cover
        every operation the committed config declares; config may override, never fill a blank."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "config", "mcp_tools.json"), encoding="utf-8-sig") as handle:
            shipped = json.load(handle)
        declared = [name for name in shipped["operations"] if not name.startswith("_")]
        missing = [name for name in declared if not mcp_registry._PURPOSE.get(name)]
        self.assertEqual(missing, [], f"operations with no purpose text: {missing}")


class InvocationGateTests(ConsoleTestCase):

    def test_console_flag_off_refuses_before_anything_is_built(self):
        with mock.patch.object(config, "MCP_CONSOLE", False):
            with self.assertRaises(mcp_console.ConsoleDisabled):
                mcp_console.run("log.list_apps", {})

    def test_denied_tool_is_refused_and_never_sent(self):
        result, session = self._run("portal.resend")
        self.assertFalse(result["ok"])
        self.assertFalse(result["called"])
        self.assertEqual(session.calls, [])
        self.assertIn("never_expose", result["error"])

    def test_unknown_operation_is_refused(self):
        result, session = self._run("log.delete_everything")
        self.assertFalse(result["called"])
        self.assertEqual(session.calls, [])

    def test_unwired_argument_refuses_rather_than_guessing_a_name(self):
        result, session = self._run("log.read", {"keyword": "timeout"})
        self.assertFalse(result["called"])
        self.assertIn("keyword", result["error"])
        self.assertEqual(session.calls, [])

    def test_wired_arguments_pass_through_with_their_names(self):
        _result, session = self._run("log.read", {"app": "postman", "file": "otx_trace.log"})
        _method, params = session.calls[-1]
        self.assertEqual(params["name"], "read_logdream_log")
        self.assertEqual(params["arguments"],
                         {"format": "json", "app": "postman", "file_name": "otx_trace.log"})

    def test_empty_argument_is_not_sent_as_an_empty_string(self):
        """An empty form field means "do not send this argument". Sending "" instead would be a
        defaulted value, and the whole no-defaulting rule exists because a wrong window returns
        nothing and nothing reads as "no problem"."""
        _result, session = self._run("log.read", {"app": "postman", "file": ""})
        self.assertEqual(session.calls[-1][1]["arguments"], {"format": "json", "app": "postman"})

    def test_transport_failure_is_reported_as_asked_but_unanswered(self):
        with mock.patch.object(mcp_client, "call",
                               side_effect=mcp_client.TransportError("logdream MCP unreachable: "
                                                                     "dns_failure", kind="dns_failure")):
            result = mcp_console.run("log.list_apps", {})
        self.assertTrue(result["called"])
        self.assertTrue(result["transport_failure"])
        self.assertEqual(result["kind"], "dns_failure")

    def test_tool_reported_error_is_not_an_ok_result(self):
        """Their tool running and complaining is a different fact from a transport failure and from
        an empty result. Collapsing them is how "the query was rejected" becomes "the log was clean"."""
        result, _session = self._run("log.list_apps", result=_tool_result("bad app", is_error=True))
        self.assertFalse(result["ok"])
        self.assertTrue(result["tool_reported_error"])
        self.assertFalse(result.get("transport_failure"))


class ExitGateTests(ConsoleTestCase):

    LEAKY = ("customer alice@example.com called from 9123 4567 about card 4111111111111111 "
             "ref MSGX-9F2K-8823")

    def test_metadata_operation_is_redacted_too(self):
        """`data_class` is OUR guess about THEIR data. Redaction must not depend on it, or a wrong
        guess becomes an exposure instead of a mislabelled badge."""
        self.assertEqual(mcp_console.catalog()["operations"]["log.list_apps"]["data_class"],
                         "metadata")
        result, _session = self._run("log.list_apps", result=_tool_result(self.LEAKY))
        self.assertNotIn("alice@example.com", result["text"])
        self.assertNotIn("4111111111111111", result["text"])
        self.assertIn("<email:", result["text"])

    def test_structured_content_is_redacted_with_its_shape_intact(self):
        body = {"entries": [{"name": "app-one", "contact": "alice@example.com"}], "count": 1}
        result, _session = self._run("log.list_apps",
                                     result=_tool_result("", structured=body))
        blob = json.dumps(result["structured"], ensure_ascii=False)
        self.assertNotIn("alice@example.com", blob)
        self.assertIn("<email:", blob)
        # Shape survives: a flattened blob would hide the very field mismatch this console diagnoses.
        self.assertEqual(result["structured"]["count"], 1)
        self.assertEqual(result["structured"]["entries"][0]["name"], "app-one")

    def test_exit_scan_finds_nothing_left(self):
        result, _session = self._run("log.read", {"app": "a", "file": "b"},
                                     result=_tool_result(self.LEAKY))
        self.assertEqual(result["exit_scan"]["sanitized_at_exit"], 0)
        self.assertGreaterEqual(result["redacted"].get("email", 0), 1)

    def test_our_endpoint_never_appears_in_a_message_we_compose(self):
        """`mcp_client`'s contract: the SERVER NAME travels, the address never does — error text is
        persisted to chat_sessions.json and rendered in the browser, so the address has to stay out
        of the message and not just out of git.

        A URL inside THEIR response body is a different thing entirely — a log line naming an
        internal service is evidence, and stripping it would damage what the operator came to read.
        """
        session = _FakeSession({"jsonrpc": "2.0", "id": 1,
                                "error": {"code": -32602, "message": "bad argument"}})

        class _RaisingSession(_FakeSession):
            def request(self, method, params=None):
                from webapp.mcp_client import _rpc_result
                return _rpc_result(self._result, f"logdream {method}")

        with mock.patch.object(mcp_client, "_session", lambda *a, **k: _RaisingSession(session._result)):
            result = mcp_console.run("log.list_apps", {})
        self.assertTrue(result["transport_failure"])
        self.assertNotIn("logs.internal", result["error"])
        self.assertIn("logdream", result["error"])

    def test_a_url_in_their_response_body_is_left_alone(self):
        result, _session = self._run("log.list_apps",
                                     result=_tool_result("GET http://payments.internal/api failed"))
        self.assertIn("payments.internal", result["text"])

    def test_render_budget_truncates_and_says_so(self):
        with mock.patch.object(config, "MCP_CONSOLE_MAX_CHARS", 50):
            result, _session = self._run("log.list_apps", result=_tool_result("x" * 500))
        self.assertEqual(len(result["text"]), 50)
        self.assertTrue(result["truncated"])

    def test_raw_ref_only_exists_when_retention_is_on(self):
        from webapp import incident_raw_store
        with mock.patch.object(incident_raw_store, "enabled", lambda: False):
            result, _session = self._run("log.list_apps", result=_tool_result("secret line"))
        self.assertNotIn("raw_ref", result)
        self.assertIn("discarded", result["storage_rule"])

    def test_raw_text_goes_to_the_owner_scoped_store_not_the_result(self):
        from webapp import incident_raw_store
        stored = {}

        def _put(owner, lines, meta=None):
            stored.update({"owner": owner, "lines": lines, "meta": meta})
            return "ref123"

        with mock.patch.object(incident_raw_store, "enabled", lambda: True), \
                mock.patch.object(incident_raw_store, "put", _put):
            session = _FakeSession(_tool_result(self.LEAKY))
            with mock.patch.object(mcp_client, "_session", lambda *a, **k: session):
                result = mcp_console.run("log.list_apps", {}, owner="browser-42")
        self.assertEqual(result["raw_ref"], "ref123")
        self.assertEqual(stored["owner"], "browser-42")
        # The store holds the original; the result does not.
        self.assertIn("alice@example.com", stored["lines"][0])
        self.assertNotIn("alice@example.com", json.dumps(result, ensure_ascii=False))

    def test_shape_report_describes_the_body_without_carrying_it(self):
        body = {"entries": [{"name": "app-one"}, {"name": "app-two"}]}
        result, _session = self._run("log.list_apps", result=_tool_result("", structured=body))
        self.assertTrue(result["shape"]["body_is_json"])
        self.assertEqual(result["shape"]["parsed"]["apps"], 2)


class RemoteDescriptionTests(unittest.TestCase):
    """Their `tools/list` descriptions are worth showing and are not ours to vouch for."""

    def test_detail_keeps_description_and_argument_names_only(self):
        detail = mcp_client._tool_detail({
            "name": "read_logdream_log",
            "description": "Read   a log\nfile.",
            "inputSchema": {"type": "object",
                            "properties": {"app": {"type": "string"}, "file_name": {}},
                            "required": ["app"]},
        })
        self.assertEqual(detail["description"], "Read a log file.")
        self.assertEqual(detail["arg_names"], ["app", "file_name"])
        self.assertEqual(detail["required_args"], ["app"])
        self.assertEqual(detail["source"], "remote tools/list")

    def test_remote_description_is_bounded(self):
        detail = mcp_client._tool_detail({"name": "t", "description": "a" * 5000})
        self.assertLessEqual(len(detail["description"]), mcp_client._MAX_REMOTE_DESCRIPTION)

    def test_a_malformed_entry_does_not_raise(self):
        detail = mcp_client._tool_detail({"name": "t", "inputSchema": "not a schema"})
        self.assertEqual(detail["arg_names"], [])
        self.assertEqual(detail["description"], "")


if __name__ == "__main__":
    unittest.main()
