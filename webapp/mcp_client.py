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
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, mcp_registry

_CLIENT_INFO = {"name": "sdlc-ai-recon", "version": "0.1"}

# An opener with an EMPTY ProxyHandler ignores HTTP_PROXY/HTTPS_PROXY entirely. RUNBOOK-60: the
# company proxy 403s every MCP call, because these are internal-infra hosts that must be reached
# directly. Using this by default means the box does not have to remember to set NO_PROXY.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(request, timeout):
    """urlopen for MCP, bypassing system proxies unless a deployment opts back in."""
    if config.MCP_USE_PROXY:
        return urllib.request.urlopen(request, timeout=timeout)
    return _DIRECT_OPENER.open(request, timeout=timeout)


class Disabled(RuntimeError):
    """The feature flag or the server's `enabled` switch is off, or no address is configured."""


class TransportError(RuntimeError):
    """We could not complete a well-formed exchange: unreachable, HTTP error, bad framing, timeout.

    Kept distinct from a tool that ran and returned nothing, because during an incident those two
    lead to opposite conclusions — "the log service is down" versus "there are no matching lines".

    `retryable` marks the subset worth one more attempt: DNS/connect/reset/timeout, i.e. the network
    did not carry the request. An HTTP 4xx is NOT retryable — the server answered, and asking again
    changes nothing except how much we hammer it.

    `kind` is the diagnosable category (`connection_refused`, `dns_failure`, `timeout`, `http_502`).
    The intranet spent a whole cycle establishing that LogDream's 8092 was REFUSED rather than
    timing out — refused means a listener problem, timeout means a network path problem, and they
    go to different owners. Naming the category means the next person does not have to re-derive it.

    The message never contains the endpoint URL. Addresses are deliberately kept out of git, but the
    exception text is what lands in `not_investigated`, which is persisted to chat_sessions.json and
    rendered in the browser — so the address has to stay out of the message too, not just out of the
    repo. The SERVER NAME is already shown everywhere (the step badges say `logdream · log.read`),
    and it is what an operator needs.
    """

    def __init__(self, message, retryable=False, kind=""):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


# Failure categories worth telling apart, longest-match first so `connection refused` is not
# swallowed by a generic `refused`.
_REASON_KINDS = (
    ("connection refused", "connection_refused"),
    ("actively refused", "connection_refused"),        # Windows wording
    ("name or service not known", "dns_failure"),
    ("getaddrinfo", "dns_failure"),
    ("nodename nor servname", "dns_failure"),
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("connection reset", "connection_reset"),
    ("certificate", "tls_failure"),
    ("ssl", "tls_failure"),
)


def _reason_kind(reason):
    """A socket error -> a short diagnosable category, never the address it failed to reach."""
    text = str(reason or "").lower()
    for marker, kind in _REASON_KINDS:
        if marker in text:
            return kind
    return "unreachable"


def _safe_detail(text):
    """An upstream error body, bounded and stripped of anything URL-shaped.

    Their error bodies are theirs: a proxy page, a stack trace, or an echo of the request can all
    carry the endpoint. Bounded and de-URL'd before it can reach the packet.
    """
    cleaned = re.sub(r"https?://\S+", "<endpoint>", str(text or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned[:200]


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


def _post(url, payload, headers, timeout, server="mcp"):
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        if value:
            request.add_header(name, value)
    try:
        return _urlopen(request, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        # 5xx is the server failing to serve a request it DID receive — worth one more try.
        # 4xx is an answer: wrong tool, no auth, bad argument. Retrying only hammers them.
        raise TransportError(f"{server} MCP returned HTTP {exc.code}: {_safe_detail(detail)}",
                             retryable=exc.code >= 500, kind=f"http_{exc.code}")
    except urllib.error.URLError as exc:
        raise TransportError(f"{server} MCP unreachable: {_reason_kind(exc.reason)}",
                             retryable=True, kind=_reason_kind(exc.reason))
    except (TimeoutError, OSError) as exc:
        raise TransportError(f"{server} MCP transport failure: {_reason_kind(exc)}",
                             retryable=True, kind=_reason_kind(exc))


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

    def __init__(self, url, timeout, server=""):
        self.url = url
        self.timeout = timeout
        self.server = server or "mcp"
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
        resp = _post(self.url, payload, self._headers(), self.timeout, self.server)
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
                        return _rpc_result(message, f"{self.server} {method}")
                raise TransportError(
                    f"{self.server} {method}: SSE stream ended with no reply to id {message_id}")
            body, truncated = _read_capped(resp)
            self.truncated = self.truncated or truncated
            if truncated:
                raise TransportError(
                    f"{self.server} {method}: response exceeded SDLC_MCP_MAX_BYTES "
                    f"({config.MCP_MAX_RESPONSE_BYTES}) and cannot be parsed as JSON. Narrow the "
                    f"query (shorter window, fewer lines) rather than raising the cap.")
            try:
                return _rpc_result(json.loads(body.decode("utf-8", "replace")),
                                   f"{self.server} {method}")
            except json.JSONDecodeError as exc:
                raise TransportError(f"{self.server} {method}: reply was not JSON ({exc})")
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

    def __init__(self, url, timeout, server=""):
        self.url = url
        self.timeout = timeout
        self.server = server or "mcp"
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
            self._stream = _urlopen(request, self.timeout)
        except urllib.error.HTTPError as exc:
            raise TransportError(
                f"{self.server} MCP SSE returned HTTP {exc.code}: "
                f"{_safe_detail(exc.read().decode('utf-8', 'replace')[:300])}",
                retryable=exc.code >= 500, kind=f"http_{exc.code}")
        except urllib.error.URLError as exc:
            raise TransportError(f"{self.server} MCP SSE unreachable: {_reason_kind(exc.reason)}",
                                 retryable=True, kind=_reason_kind(exc.reason))
        except (TimeoutError, OSError) as exc:
            raise TransportError(f"{self.server} MCP SSE transport failure: {_reason_kind(exc)}",
                                 retryable=True, kind=_reason_kind(exc))
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
                f"{self.server} MCP SSE never announced an `endpoint` event, so there is nowhere to "
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
                return _rpc_result(message, f"{self.server} {method}")
        raise TransportError(
            f"{self.server} {method}: the SSE stream closed before replying to id {message_id}")


def _session(server, timeout=None):
    url, transport = _endpoint(server)
    timeout = timeout or config.MCP_TIMEOUT
    if transport == "streamable_http":
        return _StreamableHttp(url, timeout, server)
    if transport == "sse":
        return _LegacySse(url, timeout, server)
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

    # One short, audited retry for a transport failure that did not carry the request (RUNBOOK-65:
    # a single `getaddrinfo failed` that was healthy again 3 seconds later). Safe here because every
    # allow-listed operation is a READ — the deny layer in mcp_registry is what makes that true, so
    # a retry cannot repeat a side effect. NOT retried: a tool that ran and reported an error (their
    # answer, not a network blip) and any 4xx (also an answer).
    #
    # The attempt count travels in the result and into the packet on purpose. The failure mode this
    # guards against is the one this whole module exists for: a first-attempt blip being written up
    # as "no logs found".
    attempts = 0
    last_error = None
    while attempts < max(1, config.MCP_RETRY_ATTEMPTS):
        attempts += 1
        try:
            with _session(server, timeout) as session:
                result = session.request("tools/call", {"name": tool, "arguments": params})
                truncated = getattr(session, "truncated", False)
                protocol = session.protocol
                server_info = getattr(session, "server_info", {}) or {}
            break
        except TransportError as exc:
            last_error = exc
            if not getattr(exc, "retryable", False) or attempts >= max(1, config.MCP_RETRY_ATTEMPTS):
                raise TransportError(
                    f"{exc} [after {attempts} attempt(s)]",
                    retryable=getattr(exc, "retryable", False),
                    kind=getattr(exc, "kind", "")) from None
            time.sleep(config.MCP_RETRY_DELAY)
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
        # Auditable: >1 means the first attempt failed at the transport and this result came from a
        # retry. Surfaced in the step stream and the packet so a reader can tell a flaky network
        # from a quiet one, and so nobody writes up a blip as an absence of logs.
        "attempts": attempts,
        "retried": attempts > 1,
        "retry_reason": str(last_error)[:200] if attempts > 1 and last_error else "",
        "elapsed_ms": int((time.time() - started) * 1000),
        "protocol": protocol,
        "server_info": {"name": server_info.get("name") or "",
                        "version": server_info.get("version") or ""},
        "provenance": f"mcp:{server}/{tool}",
        "timezone_warning": ("Times are passed through verbatim. CloudWatch is UTC, LogDream "
                             "defaults to Asia/Hong_Kong, the servers are GMT — a window without an "
                             "explicit zone is not evidence of anything."),
    }


# A remote tool description is REMOTE TEXT. It is worth showing — nobody documents their tool better
# than they do, and the panel exists so an operator can read what a tool is — but it arrives from a
# system we do not control, so it is bounded here and never goes anywhere near a model prompt. The
# caller renders it escaped and labelled as theirs.
_MAX_REMOTE_DESCRIPTION = 600


def _tool_detail(entry):
    """One `tools/list` entry -> what is safe and useful to show. Their text, bounded; their schema,
    reduced to argument NAMES (a full JSON Schema is neither readable nor ours to interpret)."""
    schema = entry.get("inputSchema") if isinstance(entry.get("inputSchema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return {
        "name": entry.get("name") or "",
        "description": " ".join(str(entry.get("description") or "").split())[:_MAX_REMOTE_DESCRIPTION],
        "arg_names": sorted(str(key) for key in props),
        "required_args": sorted(str(key) for key in required if isinstance(key, str)),
        # Says out loud where this text came from, so the panel cannot present it as ours.
        "source": "remote tools/list",
    }


def list_tools(server, timeout=None):
    """Their `tools/list` for one server, for CROSS-CHECKING only. Confers no call rights."""
    _require_enabled()
    with _session(server, timeout) as session:
        result = session.request("tools/list", {})
        info = getattr(session, "server_info", {}) or {}
        protocol = session.protocol
    entries = [entry for entry in (result.get("tools") or []) if isinstance(entry, dict)]
    tools = [entry.get("name") or "" for entry in entries]
    return {"server": server, "tools": sorted(name for name in tools if name),
            "count": len(tools), "protocol": protocol,
            # Kept alongside the plain name list rather than replacing it: `probe` compares names and
            # must keep doing exactly that. Descriptions are for humans reading the panel.
            "details": sorted((_tool_detail(entry) for entry in entries if entry.get("name")),
                              key=lambda detail: detail["name"]),
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
                "declared": sorted(declared), "live": [], "missing": [], "unused": [],
                "details": []}
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
        # Their own words for each live tool. Informational, bounded, and rendered as theirs.
        "details": listing.get("details") or [],
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
