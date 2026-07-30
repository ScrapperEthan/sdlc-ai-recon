"""The MCP transport.

Run against a real loopback HTTP server rather than a mocked `urlopen`, because what is actually at
risk here is protocol framing — legacy SSE answers a POST on a *different* connection, and a mock
that returns whatever we tell it to would confirm our own misunderstanding rather than the protocol.

The security-relevant assertions are the ones counting requests: a refused operation must not merely
return an error, it must never reach the network at all.
"""
import json
import os
import queue
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from webapp import config, mcp_client, mcp_registry


CONFIG = {
    "servers": {
        "cloudwatch": {"url_env": "TEST_CW_URL", "transport": "streamable_http", "enabled": True},
        "logdream": {"url_env": "TEST_LD_URL", "transport": "sse", "enabled": True},
        "portal": {"url_env": "TEST_PORTAL_URL", "transport": "?", "enabled": False},
    },
    "operations": {
        "aws.get_alarm": {"server": "cloudwatch", "tool": "get_alarm",
                           "args": {"alarm_name": "alarmName"}, "const": {}},
        "aws.window": {"server": "cloudwatch", "tool": "get_metric_window",
                        "args": {"from_time": "from", "to_time": "to", "namespace": "?"},
                        "const": {}},
        "aws.renamed": {"server": "cloudwatch", "tool": "get_alarm_v2",
                         "args": {}, "const": {}},
        "log.read": {"server": "logdream", "tool": "read_logdream_log",
                      "args": {"app": "app", "keyword": "keyword"}, "const": {"source": "hk1"}},
        "portal.sms": {"server": "portal", "tool": "query_sms_by_tracking_id",
                        "args": {"tracking_id": "trackingId"}, "const": {}},
        "danger.login": {"server": "cloudwatch", "tool": "open_portal_login",
                          "args": {}, "const": {}},
    },
    "never_expose": {"tools": ["open_portal_login"], "patterns": ["do_*"]},
}

# What the fake servers answer `tools/call` with, per test. Set by the tests.
REPLIES = {}
# Every JSON-RPC method the fake servers received, so "was a socket opened at all" is assertable.
SEEN = []


def _reply_for(method, params):
    if method == "initialize":
        return {"protocolVersion": "2025-03-26", "capabilities": {},
                "serverInfo": {"name": "fake-mcp", "version": "1"}}
    if method == "tools/list":
        return {"tools": [{"name": name} for name in REPLIES.get("tools", ["get_alarm"])]}
    if method == "tools/call":
        return REPLIES.get("call", {"content": [{"type": "text", "text": "ok"}]})
    return {}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass                                            # keep the test output clean

    # ---- streamable HTTP ----------------------------------------------------------------
    def _do_streamable(self, message, as_sse):
        method = message.get("method") or ""
        SEEN.append(method)
        if not message.get("id"):                        # a notification
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if REPLIES.get("rpc_error"):
            envelope = {"jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32603, "message": "boom on their side"}}
        else:
            envelope = {"jsonrpc": "2.0", "id": message["id"],
                        "result": _reply_for(method, message.get("params") or {})}
        blob = json.dumps(envelope)
        if REPLIES.get("oversize") and method == "tools/call":
            blob = json.dumps({"jsonrpc": "2.0", "id": message["id"],
                               "result": {"content": [{"type": "text", "text": "x" * 5000}]}})
        if as_sse:
            body = (f"event: message\ndata: {blob}\n\n").encode("utf-8")
            kind = "text/event-stream"
        else:
            body = blob.encode("utf-8")
            kind = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if method == "initialize":
            self.send_header("Mcp-Session-Id", "sess-42")
        else:
            SEEN.append("session:" + (self.headers.get("Mcp-Session-Id") or "<none>"))
        self.end_headers()
        self.wfile.write(body)

    # ---- legacy SSE --------------------------------------------------------------------
    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def _sse_write(self, frame):
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def _sse_pump(self):
        """Relay replies that the POST handler queued, until the test is done with us."""
        while True:
            try:
                blob = self.server.outbox.get(timeout=5)
            except queue.Empty:
                return
            if blob is None:
                return
            self._sse_write(f"event: message\ndata: {blob}\n\n")

    def do_GET(self):
        if self.path.startswith("/sse-noendpoint"):
            # What a non-MCP event-stream looks like: some traffic, then the stream just ends.
            self._sse_open()
            self._sse_write(": just a heartbeat, no endpoint\n\n")
        elif self.path.startswith("/sse-oddly-named"):
            # A server that labels the announcement something other than `endpoint`.
            self._sse_open()
            self._sse_write("event: session\ndata: /messages?session=1\n\n")
            self._sse_pump()
        elif self.path.startswith("/sse"):
            self._sse_open()
            self._sse_write("event: endpoint\ndata: /messages?session=1\n\n")
            self._sse_pump()
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if self.path.startswith("/messages"):
            method = message.get("method") or ""
            SEEN.append(method)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            if message.get("id"):
                envelope = {"jsonrpc": "2.0", "id": message["id"],
                            "result": _reply_for(method, message.get("params") or {})}
                self.server.outbox.put(json.dumps(envelope))
            return
        if self.path.startswith("/http-sse"):
            self._do_streamable(message, as_sse=True)
        elif self.path.startswith("/http"):
            self._do_streamable(message, as_sse=False)
        else:
            self.send_error(404)


class _ServerCase(unittest.TestCase):
    """One loopback server, a temp registry config, and the feature flag ON."""

    cw_path = "/http"

    def setUp(self):
        REPLIES.clear()
        del SEEN[:]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.outbox = queue.Queue()
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % self.server.server_address[1]

        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "mcp_tools.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(CONFIG, handle)
        self._env = mock.patch.dict(os.environ, {
            "SDLC_MCP_TOOLS": path,
            "TEST_CW_URL": base + self.cw_path,
            "TEST_LD_URL": base + "/sse",
        })
        self._env.start()
        self._flag = mock.patch.object(config, "MCP_ENABLED", True)
        self._flag.start()

    def tearDown(self):
        self.server.outbox.put(None)
        self._flag.stop()
        self._env.stop()
        self._tmp.cleanup()
        self.server.shutdown()
        self.server.server_close()


class StreamableHttpTests(_ServerCase):
    def test_a_json_reply_round_trips_through_the_abstract_operation_name(self):
        REPLIES["call"] = {"content": [{"type": "text", "text": "ALARM in ALARM state"}]}
        out = mcp_client.call("aws.get_alarm", {"alarm_name": "prodECS_x"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["tool"], "get_alarm")
        self.assertIn("ALARM state", out["text"])
        self.assertEqual(out["server_info"]["name"], "fake-mcp")
        self.assertIn("initialize", SEEN)
        self.assertIn("tools/call", SEEN)

    def test_the_session_id_from_initialize_is_echoed_on_later_requests(self):
        mcp_client.call("aws.get_alarm", {"alarm_name": "a"})
        self.assertIn("session:sess-42", SEEN)

    def test_a_tool_that_reports_failure_is_not_a_transport_failure(self):
        """During an incident these lead to opposite conclusions — "your query was wrong" versus
        "we never reached the log service" — so they must never be collapsed."""
        REPLIES["call"] = {"isError": True, "content": [{"type": "text", "text": "no such alarm"}]}
        out = mcp_client.call("aws.get_alarm", {"alarm_name": "nope"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["tool_reported_error"])
        self.assertIn("no such alarm", out["text"])

    def test_a_jsonrpc_error_is_a_transport_error_quoting_their_message(self):
        REPLIES["rpc_error"] = True
        with self.assertRaises(mcp_client.TransportError) as caught:
            mcp_client.call("aws.get_alarm", {"alarm_name": "a"})
        self.assertIn("boom on their side", str(caught.exception))

    def test_non_text_content_blocks_are_kept_not_silently_dropped(self):
        REPLIES["call"] = {"content": [{"type": "text", "text": "see chart"},
                                       {"type": "image", "data": "..."}]}
        out = mcp_client.call("aws.get_alarm", {"alarm_name": "a"})
        self.assertEqual(out["text"], "see chart")
        self.assertEqual(out["non_text_blocks"], ["image"])
        self.assertEqual(len(out["content"]), 2)

    def test_an_oversized_response_says_narrow_the_query_not_raise_the_cap(self):
        REPLIES["oversize"] = True
        with mock.patch.object(config, "MCP_MAX_RESPONSE_BYTES", 500):
            with self.assertRaises(mcp_client.TransportError) as caught:
                mcp_client.call("aws.get_alarm", {"alarm_name": "a"})
        self.assertIn("Narrow the query", str(caught.exception))

    def test_time_arguments_are_passed_verbatim_with_no_invented_default(self):
        mcp_client.call("aws.window", {"from_time": "2026-07-30T01:00:00Z", "to_time": None})
        out = mcp_client.call("aws.window", {"from_time": "2026-07-30T01:00:00Z"})
        self.assertEqual(out["params_sent"], ["from"])   # `to` absent, not back-filled
        self.assertIn("Asia/Hong_Kong", out["timezone_warning"])

    def test_the_trace_carries_parameter_names_only_never_their_values(self):
        out = mcp_client.call("log.read", {"app": "otx", "keyword": "TIMEOUT"})
        self.assertEqual(out["params_sent"], ["app", "keyword", "source"])
        self.assertNotIn("otx", json.dumps(out["params_sent"]))


class StreamableHttpOverSseTests(_ServerCase):
    """Same transport, but the server answers the POST with an event-stream instead of JSON."""

    cw_path = "/http-sse"

    def test_the_reply_is_picked_out_of_the_event_stream_by_its_id(self):
        REPLIES["call"] = {"content": [{"type": "text", "text": "streamed answer"}]}
        out = mcp_client.call("aws.get_alarm", {"alarm_name": "a"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "streamed answer")


class LegacySseTests(_ServerCase):
    def test_a_reply_arriving_on_the_held_open_get_stream_is_matched_to_its_post(self):
        """The whole reason this transport needs its own implementation: the POST returns 202 and the
        answer comes back on a different connection."""
        REPLIES["call"] = {"content": [{"type": "text", "text": "otx_trace.log line 5"}]}
        out = mcp_client.call("log.read", {"app": "otx", "keyword": "TIMEOUT"})
        self.assertTrue(out["ok"])
        self.assertIn("otx_trace.log", out["text"])
        self.assertEqual(out["server"], "logdream")

    def test_constants_from_the_config_travel_with_the_call(self):
        out = mcp_client.call("log.read", {"app": "otx", "keyword": "x"})
        self.assertIn("source", out["params_sent"])      # const source=hk1

    def test_the_announcement_is_recognised_by_shape_not_only_by_its_event_name(self):
        """We have not seen LogDream's actual framing, only that it is "legacy SSE". Keying on the
        payload looking like an address as well as on the event name means a differently-labelled
        announcement still works, and it cannot misfire: every JSON-RPC frame is JSON."""
        REPLIES["call"] = {"content": [{"type": "text", "text": "found it"}]}
        with mock.patch.dict(os.environ, {"TEST_LD_URL": os.environ["TEST_LD_URL"].replace(
                "/sse", "/sse-oddly-named")}):
            out = mcp_client.call("log.read", {"app": "otx", "keyword": "x"})
        self.assertEqual(out["text"], "found it")

    def test_a_stream_with_no_endpoint_event_explains_what_is_missing(self):
        with mock.patch.dict(os.environ, {"TEST_LD_URL": os.environ["TEST_LD_URL"].replace(
                "/sse", "/sse-noendpoint")}):
            with self.assertRaises(mcp_client.TransportError) as caught:
                mcp_client.call("log.read", {"app": "otx", "keyword": "x"})
        self.assertIn("endpoint", str(caught.exception))
        self.assertIn("nowhere to", str(caught.exception))


class RefusalsNeverTouchTheNetworkTests(_ServerCase):
    """The assertion that matters: refused work must not reach a production system at all."""

    def test_a_denied_tool_is_refused_before_any_socket_is_opened(self):
        with self.assertRaises(mcp_registry.NotAllowed):
            mcp_client.call("danger.login")
        self.assertEqual(SEEN, [])

    def test_an_unknown_operation_is_refused_before_any_socket_is_opened(self):
        with self.assertRaises(mcp_registry.NotAllowed):
            mcp_client.call("log.whatever")
        self.assertEqual(SEEN, [])

    def test_an_unwired_argument_is_refused_before_any_socket_is_opened(self):
        with self.assertRaises(mcp_registry.NotWired):
            mcp_client.call("aws.window", {"namespace": "AWS/ECS"})
        self.assertEqual(SEEN, [])

    def test_a_disabled_server_is_refused_before_any_socket_is_opened(self):
        with self.assertRaises(mcp_registry.NotAllowed):
            mcp_client.call("portal.sms", {"tracking_id": "T1"})
        self.assertEqual(SEEN, [])

    def test_there_is_no_public_way_to_call_a_tool_by_name(self):
        """`call` takes an operation, never a tool name. If a raw-name entry point is ever added,
        the deny list and the allow-list both stop being enforceable."""
        for name in dir(mcp_client):
            if name.startswith("_"):
                continue
            attr = getattr(mcp_client, name)
            if callable(attr) and getattr(attr, "__module__", "") == mcp_client.__name__:
                args = getattr(attr, "__code__", None)
                if args:
                    self.assertNotIn("tool", args.co_varnames[:args.co_argcount],
                                     f"{name}() takes a tool name")


class FeatureFlagTests(_ServerCase):
    def test_with_the_flag_off_no_call_opens_a_socket(self):
        with mock.patch.object(config, "MCP_ENABLED", False):
            for attempt in (lambda: mcp_client.call("aws.get_alarm", {"alarm_name": "a"}),
                            lambda: mcp_client.list_tools("cloudwatch")):
                with self.assertRaises(mcp_client.Disabled):
                    attempt()
        self.assertEqual(SEEN, [])

    def test_status_reports_that_readiness_is_not_permission(self):
        with mock.patch.object(config, "MCP_ENABLED", False):
            out = mcp_client.status()
        self.assertFalse(out["calling_enabled"])
        self.assertIn("not permission", out["calling_note"])
        self.assertEqual(SEEN, [])

    def test_a_missing_address_is_refused_with_the_variable_to_set(self):
        with mock.patch.dict(os.environ, {"TEST_CW_URL": ""}):
            with self.assertRaises(mcp_client.Disabled) as caught:
                mcp_client.call("aws.get_alarm", {"alarm_name": "a"})
        self.assertIn("TEST_CW_URL", str(caught.exception))
        self.assertEqual(SEEN, [])

    def test_a_non_http_address_is_refused(self):
        """A config typo like file:///etc/passwd would otherwise go straight into urlopen."""
        with mock.patch.dict(os.environ, {"TEST_CW_URL": "file:///etc/passwd"}):
            with self.assertRaises(mcp_client.Disabled) as caught:
                mcp_client.call("aws.get_alarm", {"alarm_name": "a"})
        self.assertIn("http(s)", str(caught.exception))

    def test_an_unfilled_transport_is_named_as_unfilled_not_guessed(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["servers"]["portal"]["enabled"] = True
        path = os.path.join(self._tmp.name, "portal.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        with mock.patch.dict(os.environ, {"SDLC_MCP_TOOLS": path,
                                          "TEST_PORTAL_URL": "http://127.0.0.1:1/x"}):
            with self.assertRaises(mcp_client.Disabled) as caught:
                mcp_client.call("portal.sms", {"tracking_id": "T1"})
        self.assertIn("nobody has determined it yet", str(caught.exception))


class ProbeTests(_ServerCase):
    def test_a_config_naming_a_tool_the_server_does_not_expose_is_reported(self):
        """The one failure the registry cannot see by itself: the config is authoritative about what
        we may call, but only their live server is authoritative about what exists."""
        REPLIES["tools"] = ["get_alarm", "get_metric_window", "list_something_new"]
        out = mcp_client.probe("cloudwatch")
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing"], ["get_alarm_v2", "open_portal_login"])
        self.assertIn("aws.renamed", out["missing_operations"])
        self.assertIn("do not guess", out["reason"])
        self.assertIn("list_something_new", out["unused"])

    def test_a_matching_config_probes_clean(self):
        REPLIES["tools"] = ["get_alarm", "get_metric_window", "get_alarm_v2", "open_portal_login"]
        out = mcp_client.probe("cloudwatch")
        self.assertTrue(out["ok"])
        self.assertEqual(out["missing"], [])

    def test_an_unreachable_server_probes_as_a_reason_not_an_exception(self):
        with mock.patch.dict(os.environ, {"TEST_CW_URL": "http://127.0.0.1:1/nothing"}):
            out = mcp_client.probe("cloudwatch")
        self.assertFalse(out["ok"])
        self.assertTrue(out["reason"])

    def test_listing_a_tool_grants_it_nothing(self):
        REPLIES["tools"] = ["open_portal_login", "do_the_thing", "get_alarm"]
        mcp_client.probe("cloudwatch")
        with self.assertRaises(mcp_registry.NotAllowed):
            mcp_client.call("danger.login")

    def test_status_with_probes_covers_only_enabled_servers(self):
        REPLIES["tools"] = ["get_alarm"]
        out = mcp_client.status(probe_servers=True)
        self.assertEqual(sorted(out["probes"]), ["cloudwatch", "logdream"])
        self.assertTrue(out["calling_enabled"])


if __name__ == "__main__":
    unittest.main()
