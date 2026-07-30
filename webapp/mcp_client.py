"""Transport for the colleagues' MCP servers. Stdlib only, read-only, allow-list first.

`mcp_registry` decides *whether* an operation may be called and *what it is named over there*; this
module is only the wire. Nothing here takes a tool name — `call()` takes one of our abstract
operation names and hands it to `mcp_registry.build_call`, so the deny baseline and the allow-list
cannot be bypassed by any caller, including a model that has been talked into asking for
`open_portal_login`. There is deliberately no public function that accepts a raw tool name.

## Three gates before a production system is contacted

1. `SDLC_MCP_ENABLED` — off by default. Off means no socket is opened by any path in this file.
2. The server is `enabled` in the intranet's mcp_tools.json (the box's own local copy).
3. That server's `url_env` variable is set. Addresses never live in git.

All three are separate on purpose: turning the feature on in a dev checkout must not be enough to
reach anything, and neither must pulling a config that happens to say `enabled: true`.

## Two transports, because they really are two protocols

* **Streamable HTTP** (CloudWatch, 30 tools) — one POST carries the JSON-RPC request, and the reply
  comes back either as `application/json` or as an SSE stream on that same response.
* **Legacy SSE** (LogDream, 6 tools) — a held-open `GET` receives everything, while requests go out
  as separate `POST`s to a URL the server announces in its first `endpoint` event. The reply to a
  POST does *not* come back on the POST; it arrives on the GET stream. So the GET must be opened
  first and kept open across the POST, which is why this is not a small variation on the other one.

Each call runs a full handshake and closes down afterwards — no pooled sessions. That costs two
extra round-trips per call and buys statelessness: no session id shared between users or threads,
nothing to invalidate, and a failed call cannot leave a half-initialized session behind for the next
one. Given calls are measured in seconds (RUNBOOK-55 clocked one at 26.4s), the round-trips are not
where the time goes.

## What this module refuses to do

* **No writes.** Enforced by `mcp_registry`, not by care here.
* **No time defaulting.** Three timezones coexist (CloudWatch UTC / LogDream Asia/Hong_Kong /
  server GMT), so a helpfully-invented "last 30 minutes" would silently query the wrong window and
  the model would report "no anomaly" with total confidence. Time arguments are passed exactly as
  given, or not at all.
* **No persistence, no logging of payloads.** Responses are returned to the caller and never written
  anywhere. Raw production logs must reach the session store on no path whatsoever — the sanitizing
  incident-investigator sub-agent is what stands between this module and stored history, and until
  it exists these operations are intentionally not exposed as model-callable tools.
* **No discovery-as-permission.** `probe()` reads `tools/list` to CROSS-CHECK the names the intranet
  filled in, and reports drift. A tool appearing there gains nothing.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, mcp_registry

_CLIENT_INFO = {"name": "sdlc-ai-recon", "version": "0.1"}


class Disabled(RuntimeError):
    """The feature flag or the server's `enabled` switch is off, or no address is configured."""


class TransportError(RuntimeError):
    """We could not complete a well-formed exchange: unreachable, HTTP error, bad framing, timeout.

    Kept distinct from a tool that ran and returned nothing, because during an incident those two
    lead to opposite conclusions — "the log service is down" versus "there are no matching lines".
    """


def _require_enabled():
    if not config.MCP_ENABLED:
        raise Disabled(
            "MCP calling is off (SDLC_MCP_ENABLED unset). The registry and its readiness report "
            "work without it; only opening sockets is gated.")


def _endpoint(server):
    """The address for `server`, from its env var, validated. Never from the config file."""
    spec = mcp_registry.servers().get(server)
    if not spec:
        raise Disabled(f"unknown MCP server {server!r}")
    if not spec.get("enabled"):
        raise Disabled(f"MCP server {server!r} is disabled in the intranet's mcp_tools.json")
    url = (mcp_registry.server_url(server) or "").strip()
    if not url:
        raise Disabled(
            f"MCP server {server!r} has no address: set {spec.get('url_env') or '(no url_env)'} "
            f"in the environment. Addresses are never committed.")
    parsed = urllib.parse.urlparse(url)
    # A config typo like file:///etc/passwd would otherwise be handed straight to urlopen.
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise Disabled(f"MCP server {server!r} address must be http(s), got {parsed.scheme!r}")
    return url, (spec.get("transport") or "").strip().lower()


def _read_capped(resp):
    """Body bytes, plus whether the cap cut it short. Reading unbounded is how one log query
    becomes a memory incident."""
    cap = max(1, config.MCP_MAX_RESPONSE_BYTES)
    body = resp.read(cap + 1)
    if len(body) > cap:
        return body[:cap], True
    return body, False


def _post(url, payload, headers, timeout):
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        if value:
            request.add_header(name, value)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise TransportError(f"MCP HTTP {exc.code} from {url}: {detail}")
    except urllib.error.URLError as exc:
        raise TransportError(f"MCP unreachable at {url}: {exc.reason}")
    except (TimeoutError, OSError) as exc:
        raise TransportError(f"MCP transport failure at {url}: {exc}")


def _iter_sse(stream):
    """Yield `(event_name, data_text)` per SSE framing.

    The event NAME matters here, unlike in the LLM providers' reader: the legacy transport announces
    its POST address in an `endpoint` event and carries replies in `message` events, so a parser that
    only collected `data:` lines could not tell one from the other.
    """
    event = ""
    data = []
    for raw in stream:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\n").rstrip("\r")
        if line == "":
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue                                   # comment / heartbeat
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].lstrip())
    if data:
        yield event or "message", "\n".join(data)


def _rpc_result(message, where):
    """A JSON-RPC envelope -> its `result`, or a TransportError naming the server's own complaint."""
    if not isinstance(message, dict):
        raise TransportError(f"{where}: expected a JSON-RPC object, got {type(message).__name__}")
    error = message.get("error")
    if error:
        if isinstance(error, dict):
            raise TransportError(
                f"{where}: JSON-RPC error {error.get('code')}: {error.get('message') or error}")
        raise TransportError(f"{where}: JSON-RPC error {error}")
    result = message.get("result")
    return result if isinstance(result, dict) else {}


class _StreamableHttp:
    """MCP over one POST endpoint (2025-03-26). Reply is JSON or an SSE stream on the same response."""

    transport = "streamable_http"

    def __init__(self, url, timeout):
        self.url = url
        self.timeout = timeout
        self.session_id = ""
        self.protocol = config.MCP_PROTOCOL_STREAMABLE
        self.truncated = False
        self._next_id = 0

    def _headers(self):
        return {"Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": self.session_id,
                # Spec: echo the version the server settled on, once it has told us.
                "MCP-Protocol-Version": self.protocol}

    def _send(self, method, params, expect_reply=True):
        self._next_id += 1
        message_id = self._next_id
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if expect_reply:
            payload["id"] = message_id
        resp = _post(self.url, payload, self._headers(), self.timeout)
        try:
            # The session id arrives on the initialize response and must be echoed from then on.
            found = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            if found:
                self.session_id = found
            if not expect_reply:
                resp.read(1)                            # 202 Accepted, body irrelevant
                return {}
            kind = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in kind:
                for _event, data in _iter_sse(resp):
                    if not data or data.strip() == "[DONE]":
                        continue
                    try:
                        message = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # Notifications and progress updates share the stream; only our reply has our id.
                    if isinstance(message, dict) and message.get("id") == message_id:
                        return _rpc_result(message, f"{method} via {self.url}")
                raise TransportError(
                    f"{method} via {self.url}: SSE stream ended with no reply to id {message_id}")
            body, truncated = _read_capped(resp)
            self.truncated = self.truncated or truncated
            if truncated:
                raise TransportError(
                    f"{method} via {self.url}: response exceeded SDLC_MCP_MAX_BYTES "
                    f"({config.MCP_MAX_RESPONSE_BYTES}) and cannot be parsed as JSON. Narrow the "
                    f"query (shorter window, fewer lines) rather than raising the cap.")
            try:
                return _rpc_result(json.loads(body.decode("utf-8", "replace")),
                                   f"{method} via {self.url}")
            except json.JSONDecodeError as exc:
                raise TransportError(f"{method} via {self.url}: reply was not JSON ({exc})")
        finally:
            resp.close()

    def __enter__(self):
        result = self._send("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        settled = (result.get("protocolVersion") or "").strip()
        if settled:
            self.protocol = settled
        self.server_info = result.get("serverInfo") or {}
        try:
            self._send("notifications/initialized", {}, expect_reply=False)
        except TransportError:
            # Some servers answer the notification with a non-202; the session is already usable and
            # failing the whole call over an acknowledgement would be a self-inflicted outage.
            pass
        return self

    def __exit__(self, *_exc):
        return False

    def request(self, method, params=None):
        return self._send(method, params)


class _LegacySse:
    """MCP over a held-open GET plus separate POSTs (2024-11-05).

    Ordering is load-bearing: the GET must be established before a POST is sent, because the reply
    to that POST arrives on the GET stream and a stream opened afterwards would have missed it.
    """

    transport = "sse"

    def __init__(self, url, timeout):
        self.url = url
        self.timeout = timeout
        self.protocol = config.MCP_PROTOCOL_SSE
        self.truncated = False
        self.server_info = {}
        self._stream = None
        self._events = None
        self._post_url = ""
        self._next_id = 0

    def __enter__(self):
        request = urllib.request.Request(self.url, method="GET")
        request.add_header("Accept", "text/event-stream")
        try:
            self._stream = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise TransportError(f"MCP SSE HTTP {exc.code} from {self.url}: "
                                 f"{exc.read().decode('utf-8', 'replace')[:300]}")
        except urllib.error.URLError as exc:
            raise TransportError(f"MCP SSE unreachable at {self.url}: {exc.reason}")
        except (TimeoutError, OSError) as exc:
            raise TransportError(f"MCP SSE transport failure at {self.url}: {exc}")
        self._events = _iter_sse(self._stream)
        for event, data in self._events:
            text = data.strip()
            if not text or text.startswith("{"):
                continue        # a JSON-RPC frame; nothing to POST to has been announced yet
            # Named `endpoint` by the reference implementation, but keyed on SHAPE as well as name:
            # a non-JSON payload that looks like a path or URL is an address and nothing else, so a
            # server that labels the event differently still works. Cannot misfire — every JSON-RPC
            # frame is JSON, and this branch only takes non-JSON.
            if event == "endpoint" or "/" in text:
                self._post_url = urllib.parse.urljoin(self.url, text)
                break
        if not self._post_url:
            raise TransportError(
                f"MCP SSE at {self.url} never announced an `endpoint` event, so there is nowhere to "
                f"POST requests. Either it is not an MCP SSE server or it closed the stream early.")
        result = self.request("initialize", {
            "protocolVersion": self.protocol,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        settled = (result.get("protocolVersion") or "").strip()
        if settled:
            self.protocol = settled
        self.server_info = result.get("serverInfo") or {}
        try:
            self._notify("notifications/initialized")
        except TransportError:
            pass
        return self

    def __exit__(self, *_exc):
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:                            # noqa: BLE001 — closing must not raise
                pass
            self._stream = None
        return False

    def _notify(self, method, params=None):
        resp = _post(self._post_url, {"jsonrpc": "2.0", "method": method, "params": params or {}},
                     {"Accept": "text/event-stream"}, self.timeout)
        resp.close()

    def request(self, method, params=None):
        self._next_id += 1
        message_id = self._next_id
        resp = _post(self._post_url,
                     {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}},
                     {"Accept": "text/event-stream"}, self.timeout)
        resp.close()                                     # 202; the answer comes on the GET stream
        for _event, data in self._events:
            if not data or data.strip() == "[DONE]":
                continue
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == message_id:
                return _rpc_result(message, f"{method} via {self.url}")
        raise TransportError(
            f"{method} via {self.url}: the SSE stream closed before replying to id {message_id}")


def _session(server, timeout=None):
    url, transport = _endpoint(server)
    timeout = timeout or config.MCP_TIMEOUT
    if transport == "streamable_http":
        return _StreamableHttp(url, timeout)
    if transport == "sse":
        return _LegacySse(url, timeout)
    raise Disabled(
        f"MCP server {server!r} has transport {transport!r}; this client speaks "
        f"'streamable_http' and 'sse'. The intranet sets this in mcp_tools.json from what the "
        f"endpoint actually is — an unfilled '?' means nobody has determined it yet.")


def _flatten_content(result):
    """MCP tool result -> (text, blocks, non_text_kinds).

    Text is joined for the caller's convenience; the blocks are kept because an image or a resource
    link is evidence too and dropping it silently would hide it.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return "", [], []
    texts, kinds = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type") or ""
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        else:
            kinds.append(kind or "unknown")
    return "\n".join(texts), content, sorted(set(kinds))


def call(operation, args=None, timeout=None):
    """Run one abstract operation. Returns a result dict; raises only for refusals and transport.

    `ok=False` with `tool_reported_error=True` means their tool ran and reported a problem — a
    different fact from a TransportError, and the two must not be collapsed: one says the query was
    wrong, the other says we never asked.
    """
    _require_enabled()
    server, tool, params = mcp_registry.build_call(operation, args)   # allow-list + deny + naming
    started = time.time()
    with _session(server, timeout) as session:
        result = session.request("tools/call", {"name": tool, "arguments": params})
        truncated = getattr(session, "truncated", False)
        protocol = session.protocol
        server_info = getattr(session, "server_info", {}) or {}
    text, blocks, other_kinds = _flatten_content(result)
    is_error = bool(result.get("isError"))
    return {
        "ok": not is_error,
        "tool_reported_error": is_error,
        "operation": operation,
        "server": server,
        "tool": tool,
        # NAMES only. Values are ours (app, file, window, keywords), but echoing them into a trace
        # that may be shown or stored is a habit that eventually echoes something it shouldn't.
        "params_sent": sorted(params),
        "text": text,
        "content": blocks,
        "structured": result.get("structuredContent") if isinstance(
            result.get("structuredContent"), (dict, list)) else None,
        "non_text_blocks": other_kinds,
        "truncated": truncated,
        "elapsed_ms": int((time.time() - started) * 1000),
        "protocol": protocol,
        "server_info": {"name": server_info.get("name") or "",
                        "version": server_info.get("version") or ""},
        "provenance": f"mcp:{server}/{tool}",
        "timezone_warning": ("Times are passed through verbatim. CloudWatch is UTC, LogDream "
                             "defaults to Asia/Hong_Kong, the servers are GMT — a window without an "
                             "explicit zone is not evidence of anything."),
    }


def list_tools(server, timeout=None):
    """Their `tools/list` for one server, for CROSS-CHECKING only. Confers no call rights."""
    _require_enabled()
    with _session(server, timeout) as session:
        result = session.request("tools/list", {})
        info = getattr(session, "server_info", {}) or {}
        protocol = session.protocol
    tools = [entry.get("name") or "" for entry in (result.get("tools") or [])
             if isinstance(entry, dict)]
    return {"server": server, "tools": sorted(name for name in tools if name),
            "count": len(tools), "protocol": protocol,
            "server_info": {"name": info.get("name") or "", "version": info.get("version") or ""}}


def probe(server, timeout=None):
    """Connect, read `tools/list`, and cross-check it against what the config claims.

    This is the check that catches the failure the registry cannot see: a tool name filled in from a
    stale `tools/list`, or a rename on their side. The config is authoritative about what we are
    ALLOWED to call; only their live server is authoritative about what EXISTS.
    """
    cfg = mcp_registry.load()
    declared = {spec.get("tool") or "": name
                for name, spec in mcp_registry.operations(cfg).items()
                if isinstance(spec, dict) and (spec.get("tool") or "") != mcp_registry.UNSET
                and (spec.get("server") or "") == server}
    declared.pop("", None)
    try:
        listing = list_tools(server, timeout)
    except (Disabled, TransportError) as exc:
        return {"server": server, "ok": False, "reason": str(exc),
                "declared": sorted(declared), "live": [], "missing": [], "unused": []}
    live = set(listing["tools"])
    missing = sorted(name for name in declared if name not in live)
    return {
        "server": server,
        "ok": not missing,
        "reason": "" if not missing else (
            "the config names tools this server does not expose — a rename or a stale tools/list. "
            "Fix the intranet's mcp_tools.json; do not guess a replacement name."),
        "declared": sorted(declared),
        "live": listing["tools"],
        "missing": missing,
        "missing_operations": sorted(declared[name] for name in missing),
        # Informational only: what they expose that we have not wired. Listing a tool here grants it
        # nothing — several of these are deliberately never going to be wired.
        "unused": sorted(live - set(declared)),
        "protocol": listing["protocol"],
        "server_info": listing["server_info"],
    }


def status(probe_servers=False, timeout=None):
    """Registry readiness plus, optionally, a live cross-check. The box's verification surface."""
    out = dict(mcp_registry.summary())
    out["calling_enabled"] = bool(config.MCP_ENABLED)
    if not config.MCP_ENABLED:
        out["calling_note"] = ("SDLC_MCP_ENABLED is unset, so nothing can be called even where the "
                               "config reports `ready`. Readiness is about wiring, not permission.")
    if not probe_servers:
        return out
    out["probes"] = {name: probe(name, timeout)
                     for name, spec in mcp_registry.servers().items() if spec.get("enabled")}
    return out
