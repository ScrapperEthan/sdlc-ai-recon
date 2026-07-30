#!/usr/bin/env python3
"""Stdlib web server: serves the chat UI and a POST /api/chat endpoint.

Run from the workspace root (where mirror/, recon_out/, index/ live):
    python -m webapp.server
    # test with no model first:
    LLM_MOCK=1 python -m webapp.server
"""
import json
import os
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import agent, config, session_store, llm_routes, llm_credentials, llm, mcp_client
from retriever import code as rcode, config as rconfig

HERE = os.path.dirname(__file__)
INDEX = os.path.join(HERE, "static", "index.html")

# Generic, sanitized message for any connect/select/register probe failure. Deliberately never the
# raw exception str() -- a provider's own error can embed the upstream response body or a full
# endpoint URL (see e.g. copilot_responses.chat's "copilot-api unreachable at <url>: ..."), and
# those must never reach the client (spec: no Token/Authorization/proxy-credential/URL/upstream body).
_LLM_PROBE_ERROR = "could not verify that endpoint/model -- check the connection and try again"


def _sanitize_probe_failure(error, default_status=400):
    """(status, body) for a failed connect/select/register probe or models() listing -- maps the
    provider-NEUTRAL `llm.Llm*Error` taxonomy (never a `llm_providers/*` exception type -- this file
    must not import one, see docs/specs/copilot-dynamic-model-selector-external-diff-zh.md §6.4) to
    real HTTP semantics: 401/403/429 (with `retry_after` when the provider supplied one). Anything
    else collapses to a flat, sanitized `default_status` -- the message is always the same generic
    string, never the raw exception (which can embed a URL or upstream body)."""
    if isinstance(error, llm.LlmAuthError):
        return 401, {"error": _LLM_PROBE_ERROR}
    if isinstance(error, llm.LlmForbiddenError):
        return 403, {"error": _LLM_PROBE_ERROR}
    if isinstance(error, llm.LlmRateLimitError):
        # Keep the message sanitized, but preserve enough *non-secret* structure for the UI to
        # offer an honest retry action instead of treating Copilot's 429 as a generic failure.
        body = {"error": _LLM_PROBE_ERROR, "code": "copilot_rate_limit", "retryable": True}
        if error.retry_after is not None:
            body["retry_after"] = error.retry_after
        return 429, body
    return default_status, {"error": _LLM_PROBE_ERROR}


def _pick_default_model(listing):
    """From an `llm.models()` result, the model to try first: the provider's own default -- but only
    if it's actually one of the listed ids (a provider's `default_model` and its `models` list can
    disagree, e.g. after a deploy changed the default before the list caught up) -- else the first
    listed model, else "" (let the provider fall back to its own configured default)."""
    listing = listing or {}
    ids = [
        (entry.get("id") or "").strip()
        for entry in listing.get("models") or []
        if isinstance(entry, dict) and (entry.get("id") or "").strip()
    ]
    default_model = (listing.get("default_model") or "").strip()
    if default_model and default_model in ids:
        return default_model
    return ids[0] if ids else ""


def _pick_token_initial_model(listing):
    """Initial Token-mode selection: Copilot's ``auto`` if advertised, then a valid provider
    default, then the first advertised model. ``models()`` is an authenticated discovery call but
    does not prove an individual completion, so callers return ``model_verified=False`` rather
    than claiming a chat probe took place."""
    models = listing.get("models") if isinstance(listing, dict) else []
    ids = {
        (entry.get("id") or "").strip()
        for entry in models or []
        if isinstance(entry, dict)
    }
    if "auto" in ids:
        return "auto"
    return _pick_default_model(listing)


def _probe_llm(override, tools=None):
    """Bind `override` for one throwaway chat call to confirm the endpoint+model actually work.

    Passes `tools` (the real MDC tool schema, when the caller has one -- see `agent.tools.TOOLS`)
    rather than always probing with no tools at all, so the probe exercises the same request shape
    real chat turns use, not a simplified one a real endpoint might accept differently. Validates the
    response is actually a well-formed assistant message -- a provider returning None, a malformed
    dict, or a non-assistant role must count as a failed probe, not a silent success.

    Raises on any failure; callers decide what to roll back. Never touches any store itself."""
    otoken = config.set_llm_override(override)
    try:
        message = llm.chat(
            [{"role": "user", "content": "Reply with exactly OK."}],
            tools=tools,
            temperature=0,
        )
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise RuntimeError("invalid assistant response")
    finally:
        config.reset_llm_override(otoken)


def _list_models(override):
    """Bind `override` (or None for the shared default) for one `llm.models()` call."""
    otoken = config.set_llm_override(override)
    try:
        return llm.models()
    finally:
        config.reset_llm_override(otoken)


def proxy_fetch(url, timeout=30):
    """GET `url` (the retrieval service, loopback) and return (status, content_type, body_bytes).
    Never raises on an HTTP error — relays it — and turns a dead upstream into a clear 502 so the
    single-entry chat degrades gracefully instead of throwing."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            return resp.status, ctype, resp.read()
    except urllib.error.HTTPError as e:
        ctype = e.headers.get("Content-Type", "application/json; charset=utf-8")
        return e.code, ctype, e.read()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        body = json.dumps({"error": f"retrieval service unavailable: {reason}",
                           "hint": "start it with: python retrieval_service.py"},
                          ensure_ascii=False).encode("utf-8")
        return 502, "application/json; charset=utf-8", body


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "_new_uid", None):
            self.send_header("Set-Cookie", self._uid_cookie_header(self._new_uid))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) or b"{}"
        return json.loads(raw_body)

    def _user_token(self):
        """Who is this request from — the pairing token that selects their LLM endpoint.
        Header first (the frontend sends it), cookie as a fallback. Empty => env-default LLM."""
        header = self.headers.get("X-SDLC-User-Token")
        if header and header.strip():
            return header.strip()
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sdlc_token":
                return value.strip()
        return ""

    @staticmethod
    def _uid_cookie_header(uid):
        return f"sdlc_uid={uid}; Path=/; Max-Age=31536000; SameSite=Lax"

    def _describe_llm(self, user_token):
        """Which LLM this browser is bound to, for the 'my LLM' panel.

        Flag OFF (SDLC_LLM_TOKEN_MODE unset): byte-for-byte the original behaviour -- a straight
        `llm_routes.describe` call, no new keys, no credential-store lookup at all. Flag ON: also
        checks the RAM-only paste-token credential store (internal beta, see
        docs/specs/copilot-token-direct-mode.md) and annotates the reply with `mode`. Never echoes
        any token/credential secret -- both `describe()` helpers are secret-free by construction."""
        if not config.LLM_TOKEN_MODE_ENABLED:
            return llm_routes.describe(user_token)

        tunnel = llm_routes.describe(user_token)
        if tunnel.get("registered"):
            return {**tunnel, "mode": "tunnel", "token_mode_available": True}
        credential = llm_credentials.describe(user_token, owner_uid=self._uid)
        if credential.get("connected"):
            return {"registered": True, "mode": "copilot_token", "label": "Copilot (token mode)",
                    "token_mode_available": True, "model": credential.get("selected_model") or ""}
        if llm_credentials.is_credential_id(user_token):
            # This browser's token LOOKS like one of our credentials but doesn't resolve (RAM
            # cleared by a restart, disconnected elsewhere, or a 401/403 auto-disconnect -- these
            # all look identical on purpose, no oracle for which one it was). Reporting "shared"
            # here would be misleading: chat requests for this token are still pinned to the direct
            # provider and fail closed (see _resolve_llm_override), NOT silently using the shared
            # default -- the UI needs to say "reconnect", not imply nothing is wrong.
            return {"registered": False, "mode": "copilot_token", "label": "Copilot token",
                    "token_mode_available": True, "model": "", "reconnect_required": True}
        return {"registered": False, "mode": "shared", "token_mode_available": True,
                "model": config.llm_default_model()}

    def _resolve_llm_override(self, user_token):
        """Bind this request to the caller's own LLM endpoint/provider for the whole agent turn.

        Flag OFF: byte-for-byte the original behaviour -- a straight `llm_routes.resolve` call
        (tunnel override or None -> env default), no credential-store lookup at all. Flag ON: when
        the token isn't a registered tunnel, also checks the RAM-only paste-token credential store,
        scoped to THIS caller (`owner_uid=self._uid` -- a credential belonging to someone else never
        resolves here, same fail-closed check the store enforces itself).

        Fail-closed, not fail-open: if `user_token` merely LOOKS like one of our credential ids
        (`llm_credentials.is_credential_id`) but no longer resolves -- disconnected, or never
        belonged to this owner -- the request is STILL pinned to `github_copilot_direct` with that
        (now-dead) credential_id, not silently released back to the shared env-default LLM. The
        provider's own credential lookup then raises (CredentialError), so the caller sees a loud
        failure instead of quietly spending the shared/default endpoint's quota. `credential_id` +
        `credential_owner_uid` are threaded through so the provider (internal-owned) can re-verify
        ownership itself via `llm_credentials.resolve(credential_id, owner_uid=config.LLM_CREDENTIAL_OWNER_UID)`
        rather than only trusting this check.

        An unregistered tunnel token that also isn't a credential id resolves to None -- i.e. falls
        back to the shared env-default LLM, exactly as before."""
        override = llm_routes.resolve(user_token)
        if override is not None or not config.LLM_TOKEN_MODE_ENABLED:
            return override
        record = llm_credentials.resolve(user_token, owner_uid=self._uid)
        if record is not None or llm_credentials.is_credential_id(user_token):
            override = {"mode": "copilot_token", "provider": "github_copilot_direct",
                        "credential_id": user_token, "credential_owner_uid": self._uid}
            model = record.get("selected_model") if record else ""
            if model:
                override["model"] = model
                override["selected_model"] = model
            return override
        return None

    def _auto_disconnect_on_auth_failure(self, override):
        """A previously-connected Token-mode credential that starts getting rejected (401/403) by
        the real Copilot endpoint mid-session -- e.g. revoked after connecting, or the RAM store
        restarted and something raced a stale id back in -- must not keep reporting "Connected"
        while every real chat silently fails. Disconnect it (owner-bound) so the next
        `/api/llm/me` check reports `reconnect_required` instead of a stale/misleading state."""
        if override and override.get("mode") == "copilot_token":
            credential_id = override.get("credential_id")
            if credential_id:
                llm_credentials.disconnect(credential_id, owner_uid=self._uid)

    def _resolve_uid(self):
        """Who owns this browser's sessions/feedback — separate from `_user_token` (which LLM to
        call). No login: an opaque id issued once via cookie on first visit, just enough that one
        tester can't list or read another tester's chat history and feedback. Sets `self._new_uid`
        so `_send` mints the cookie on a first visit; already-cookied requests get None (no re-send)."""
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sdlc_uid" and value.strip():
                self._uid, self._new_uid = value.strip(), None
                return
        self._uid = self._new_uid = uuid.uuid4().hex

    def do_GET(self):
        self._resolve_uid()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/static/vendor/mermaid.min.js":
            # Locally vendored (air-gapped, no CDN). Absent until dropped in -> 404, and the page
            # degrades to showing mermaid source as text.
            vendor = os.path.join(HERE, "static", "vendor", "mermaid.min.js")
            try:
                with open(vendor, "rb") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"mermaid.min.js not vendored yet", "text/plain; charset=utf-8")
        elif path == "/api/source":
            qs = parse_qs(urlparse(self.path).query)
            relpath = (qs.get("path") or [""])[0]
            raw_line = (qs.get("line") or [""])[0]
            try:
                line = int(raw_line) if raw_line else None
            except ValueError:
                line = None
            try:
                self._send_json(200, rcode.read_window(relpath, line))
            except ValueError:
                self._send_json(403, {"error": "forbidden path"})
            except FileNotFoundError:
                self._send_json(404, {"error": "not found"})
        elif path == "/api/sessions":
            self._send_json(200, {"sessions": session_store.list_sessions(self._uid)})
        elif path == "/api/index-status":
            status_path = os.path.join(rconfig.INDEX_DIR, "last_indexed.json")
            try:
                with open(status_path, encoding="utf-8-sig") as handle:
                    payload = json.load(handle)
            except FileNotFoundError:
                payload = {"available": False, "error": "index freshness metadata not found"}
            except (OSError, json.JSONDecodeError) as e:
                payload = {"available": False, "error": f"invalid index freshness metadata: {e}"}
            else:
                payload["available"] = True
            self._send_json(200, payload)
        elif path == "/api/usage":
            self._send_json(200, session_store.usage_summary())
        elif path == "/api/feedback":
            # Flat log of every 👍/👎 + comment on the CALLER's OWN sessions (see session_store.list_feedback).
            self._send_json(200, {"feedback": session_store.list_feedback(self._uid)})
        elif path == "/api/llm/me":
            # Which LLM endpoint this browser is bound to (its own, or the env default).
            self._send_json(200, self._describe_llm(self._user_token()))
        elif path == "/api/llm/models":
            # Model list for whichever endpoint this browser is currently bound to (tunnel, token,
            # or the shared default) -- always available, not gated by SDLC_LLM_TOKEN_MODE.
            override = self._resolve_llm_override(self._user_token())
            try:
                listing = _list_models(override)
            except Exception as e:  # noqa: BLE001 -- never relay the raw provider error (may embed a URL)
                status, body = _sanitize_probe_failure(e, default_status=502)
                if (override and override.get("mode") == "copilot_token"
                        and isinstance(e, (llm.LlmAuthError, llm.LlmForbiddenError))):
                    self._auto_disconnect_on_auth_failure(override)
                    body["reconnect_required"] = True
                self._send_json(status, body)
            else:
                self._send_json(200, listing)
        elif path.startswith("/api/sessions/"):
            session_id = unquote(path.removeprefix("/api/sessions/"))
            try:
                session = session_store.get_session(session_id, self._uid)
            except KeyError:
                self._send_json(404, {"error": f"Session not found: {session_id}"})
            else:
                self._send_json(200, session)
        elif path == "/api/mcp/status":
            # The box's verification surface for RUNBOOK-58 onward. Wiring readiness always; a live
            # cross-check of the declared tool names against each server's own tools/list only with
            # ?probe=1, since that opens connections to production systems.
            qs = parse_qs(urlparse(self.path).query)
            want_probe = (qs.get("probe") or [""])[0] not in ("", "0", "false")
            try:
                self._send_json(200, mcp_client.status(probe_servers=want_probe))
            except Exception as e:  # noqa: BLE001 -- a status page must not 500 on a wiring problem
                self._send_json(200, {"error": str(e), "calling_enabled": bool(config.MCP_ENABLED)})
        elif path == "/health":
            # One unified health check for the single entry: this app + the retrieval upstream.
            self._send_json(200, self._unified_health())
        elif path.startswith("/api/"):
            self._send(404, b"not found", "text/plain")
        else:
            # Single entry: reverse-proxy everything else (arch/impact/coverage pages + their data
            # endpoints) to the retrieval service, so users only ever hit this one port.
            status, ctype, body = proxy_fetch(config.RETRIEVAL_UPSTREAM + self.path)
            self._send(status, body, ctype)

    def _unified_health(self):
        status, _ctype, body = proxy_fetch(config.RETRIEVAL_UPSTREAM + "/health", timeout=5)
        try:
            retrieval = json.loads(body)
        except (ValueError, TypeError):
            retrieval = {"available": False}
        return {"ok": status == 200, "webapp": "ok",
                "retrieval_upstream": config.RETRIEVAL_UPSTREAM, "retrieval": retrieval}

    def do_POST(self):
        self._resolve_uid()
        path = urlparse(self.path).path
        allowed = ["/api/chat", "/api/chat/stream", "/api/sessions", "/api/feedback",
                   "/api/llm/register", "/api/llm/select-model", "/api/llm/tunnel-models"]
        if config.LLM_TOKEN_MODE_ENABLED:
            # Internal beta only (see docs/specs/copilot-token-direct-mode.md) -- these two routes
            # don't exist (plain 404, same as any other unknown path) unless the flag is on.
            allowed += ["/api/llm/connect-token", "/api/llm/disconnect-token"]
        if path not in allowed:
            self._send(404, b"not found", "text/plain")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/sessions":
                session = session_store.create_session(req.get("title") or "New session", self._uid)
                self._send_json(201, session)
                return

            if path == "/api/llm/register":
                # A user binds their own local LLM (reached via their reverse-tunnel loopback port).
                # Probe BEFORE persisting: a bad base_url/model/api_key must never land in
                # llm_routes.json as if it worked.
                try:
                    base_url = llm_routes.validate_base_url(req.get("base_url"))
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                model = (req.get("model") or "").strip()
                api_key = req.get("api_key") or ""
                provider = (req.get("provider") or "").strip()

                route_token = (req.get("token") or "").strip()
                previous_credential_id = (req.get("previous_credential_id") or "").strip()
                # Defense in depth: a credential-shaped token must NEVER become a tunnel route
                # token's identity -- llm_routes.register()'s token namespace and
                # llm_credentials.connect()'s ct_-prefixed namespace must stay disjoint. Whether it
                # arrived as `previous_credential_id` (the current frontend, switching Token ->
                # Tunnel) or -- an older frontend, or a manual API caller -- still riding in `token`,
                # treat it the same way: "the token-mode credential to retire", never "reuse this id
                # for the new route".
                if llm_credentials.is_credential_id(route_token):
                    previous_credential_id = route_token
                    route_token = ""

                candidate = {"base_url": base_url}
                if model:
                    candidate["model"] = model
                if api_key:
                    candidate["api_key"] = api_key
                if provider:
                    candidate["provider"] = provider
                if not model:
                    try:
                        model = _pick_default_model(_list_models(candidate))
                    except Exception:  # noqa: BLE001 -- listing is best-effort; the probe below is what gates persistence
                        model = ""
                    if model:
                        candidate["model"] = model
                try:
                    _probe_llm(candidate, tools=agent.tools.TOOLS)
                except Exception as e:  # noqa: BLE001 -- never relay the raw provider error
                    status, body = _sanitize_probe_failure(e)
                    self._send_json(status, body)
                    return
                try:
                    record = llm_routes.register(
                        base_url, model, api_key,
                        req.get("label") or "", provider, route_token or None,
                    )
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                # Only once the new tunnel route is actually persisted: retire the old Token-mode
                # credential it's replacing (owner-bound -- never someone else's). If probe/register
                # above had failed, the old credential is untouched, so the user isn't locked out of
                # both.
                if llm_credentials.is_credential_id(previous_credential_id):
                    llm_credentials.disconnect(previous_credential_id, owner_uid=self._uid)
                self._send_json(200, record)
                return

            if path == "/api/llm/connect-token":
                # Internal beta (SDLC_LLM_TOKEN_MODE): the tester pastes their own `.copilot_token`;
                # it lands ONLY in the RAM-only llm_credentials store (never a file, never logged) --
                # see webapp/llm_credentials.py. The browser then sends the returned credential_id
                # back as its X-SDLC-User-Token, same header/pairing-token mechanism tunnel mode
                # already uses (see _resolve_llm_override).
                #
                # Only report "Connected" once a REAL llm.models() call against this credential has
                # succeeded (an authenticated call in its own right -- a bad/revoked token fails it
                # exactly as it would a chat call) -- a token that merely looks non-blank (e.g.
                # copy-pasted wrong, revoked, no Copilot entitlement) must never be reported as
                # connected. Deliberately does NOT also spend a real llm.chat() completion here --
                # that would burn the user's Copilot quota on every connect for no additional
                # verification. On any failure the freshly-created credential is deleted, not left
                # dangling in RAM.
                try:
                    credential_id = llm_credentials.connect(req.get("token"), owner_uid=self._uid)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                base_override = {"mode": "copilot_token", "provider": "github_copilot_direct",
                                  "credential_id": credential_id, "credential_owner_uid": self._uid}
                try:
                    listing = _list_models(base_override)
                    model = _pick_token_initial_model(listing)
                    if not model:
                        raise RuntimeError("no available model")
                except Exception as e:  # noqa: BLE001 -- never relay the raw provider error
                    llm_credentials.disconnect(credential_id, owner_uid=self._uid)
                    status, body = _sanitize_probe_failure(e)
                    self._send_json(status, body)
                    return
                llm_credentials.set_selected_model(credential_id, model, owner_uid=self._uid)
                # Return the listing we just fetched so the browser can fill its selector without
                # immediately issuing a redundant /api/llm/models request. Listing confirms
                # credential access, not a successful completion for this exact model.
                self._send_json(200, {
                    "credential_id": credential_id,
                    "connected": True,
                    "models": listing.get("models") or [],
                    "default_model": listing.get("default_model") or "",
                    "model": model,
                    "selected_model": model,
                    "model_verified": False,
                })
                return

            if path == "/api/llm/disconnect-token":
                credential_id = (req.get("credential_id") or "").strip() or self._user_token()
                llm_credentials.disconnect(credential_id, owner_uid=self._uid)
                self._send_json(200, {"ok": True})
                return

            if path == "/api/llm/select-model":
                # Switch the model for whichever endpoint this browser is currently bound to (tunnel
                # route or, in token mode, its credential). Re-confirms the model is still in the
                # provider's own list BEFORE persisting anything -- a failed switch leaves the
                # previously-confirmed model untouched. Tunnel switches also re-probe with a real
                # llm.chat() call (an arbitrary user-supplied endpoint, same as register()); Token
                # mode does not -- the models() listing check above is already a real authenticated
                # call, so switching models never spends a Copilot completion.
                user_token = self._user_token()
                new_model = (req.get("model") or "").strip()
                if not new_model:
                    self._send_json(400, {"error": "model is required"})
                    return

                tunnel_override = llm_routes.resolve(user_token)
                # resolve(..., owner_uid=self._uid) is the SAME fail-closed ownership check
                # llm_credentials enforces everywhere else -- a credential_id that exists but
                # belongs to someone else comes back None here, indistinguishable from "no such
                # credential" (never reveals which case it was).
                credential_record = (llm_credentials.resolve(user_token, owner_uid=self._uid)
                                     if tunnel_override is None and config.LLM_TOKEN_MODE_ENABLED else None)
                if tunnel_override is not None:
                    candidate = dict(tunnel_override, model=new_model, selected_model=new_model)
                elif credential_record is not None:
                    candidate = {"mode": "copilot_token", "provider": "github_copilot_direct",
                                 "credential_id": user_token, "credential_owner_uid": self._uid,
                                 "model": new_model, "selected_model": new_model}
                else:
                    self._send_json(400, {"error": "connect an LLM before choosing a model"})
                    return

                try:
                    listing = _list_models(candidate)
                except Exception as e:  # noqa: BLE001
                    status, body = _sanitize_probe_failure(e, default_status=502)
                    if (candidate.get("mode") == "copilot_token"
                            and isinstance(e, (llm.LlmAuthError, llm.LlmForbiddenError))):
                        self._auto_disconnect_on_auth_failure(candidate)
                        body["reconnect_required"] = True
                    self._send_json(status, body)
                    return
                available_ids = {(m.get("id") or "").strip() for m in listing.get("models") or []}
                if new_model not in available_ids:
                    self._send_json(400, {"error": "model is no longer available; refresh the model list"})
                    return

                if tunnel_override is not None:
                    try:
                        _probe_llm(candidate, tools=agent.tools.TOOLS)
                    except Exception as e:  # noqa: BLE001 -- never relay the raw provider error; old model stays active
                        status, body = _sanitize_probe_failure(e)
                        self._send_json(status, body)
                        return
                    llm_routes.register(
                        tunnel_override["base_url"], new_model, tunnel_override.get("api_key") or "",
                        "", tunnel_override.get("provider") or "", user_token,
                    )
                else:
                    llm_credentials.set_selected_model(user_token, new_model, owner_uid=self._uid)
                self._send_json(200, {"ok": True, "model": new_model,
                                      "selected_model": new_model, "model_verified": False})
                return

            if path == "/api/llm/tunnel-models":
                # Read-only discovery for the tunnel panel's 'Load models' button, BEFORE a tunnel is
                # registered -- never persists anything (see llm_routes.list_remote_models).
                try:
                    listing = llm_routes.list_remote_models(req.get("base_url"), req.get("api_key") or "")
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                except RuntimeError:
                    self._send_json(502, {"error": _LLM_PROBE_ERROR})
                else:
                    self._send_json(200, listing)
                return

            if path == "/api/feedback":
                try:
                    feedback = session_store.set_feedback(
                        req.get("session_id"),
                        req.get("message_index"),
                        req.get("vote") or "",
                        req.get("comment") or "",
                        self._uid,
                    )
                except KeyError:
                    self._send_json(404, {"error": "session not found"})
                except (ValueError, IndexError, TypeError) as e:
                    self._send_json(400, {"error": str(e)})
                else:
                    self._send_json(200, {"ok": True, "feedback": feedback})
                return

            question = (req.get("question") or "").strip()
            if not question:
                self._send_json(400, {"error": "Question is required"})
                return

            session_id = req.get("session_id")
            if session_id:
                try:
                    history = session_store.history_for_agent(session_id, self._uid)
                except KeyError:
                    self._send_json(404, {"error": f"Session not found: {session_id}"})
                    return
            else:
                session_id = session_store.create_session(owner=self._uid)["id"]
                history = []

            # Bind this request to the caller's own LLM endpoint/provider (their reverse-tunnel
            # loopback port, or -- token mode only -- their paste-token Copilot credential) for the
            # whole agent turn; falls back to the env default when unbound. Each request thread has
            # its own context, so users never share an endpoint.
            override = self._resolve_llm_override(self._user_token())

            if path == "/api/chat/stream":
                otoken = config.set_llm_override(override)
                try:
                    self._send_chat_stream(session_id, question, history, self._uid, override)
                finally:
                    config.reset_llm_override(otoken)
                return

            otoken = config.set_llm_override(override)
            try:
                result = agent.answer(question, history)
            except (llm.LlmAuthError, llm.LlmForbiddenError) as e:
                # Revoked/expired mid-session (or forbidden) -- disconnect so the credential stops
                # claiming "Connected" while every real chat would keep silently failing.
                self._auto_disconnect_on_auth_failure(override)
                status, body = _sanitize_probe_failure(e)
                self._send_json(status, {**body, "reconnect_required": True})
                return
            except llm.LlmRateLimitError as e:
                status, body = _sanitize_probe_failure(e)
                self._send_json(status, body)
                return
            finally:
                config.reset_llm_override(otoken)
            session = session_store.append_exchange(
                session_id,
                question,
                result.get("answer") or "",
                result.get("tool_trace"),
                result.get("usage"),
                result.get("citations"),
                result.get("views"),
                owner=self._uid,
            )
            result["session"] = {
                "id": session["id"],
                "title": session["title"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
                "message_count": session["message_count"],
                "href": f"/api/sessions/{quote(session['id'])}",
            }
            self._send_json(200, result)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON request body"})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": str(e)})

    def _send_chat_stream(self, session_id, question, history, uid, override=None):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        if getattr(self, "_new_uid", None):
            self.send_header("Set-Cookie", self._uid_cookie_header(self._new_uid))
        self.end_headers()
        self.close_connection = True

        def emit(payload):
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        try:
            for event in agent.answer_events(question, history):
                if event.get("type") == "done":
                    session = session_store.append_exchange(
                        session_id,
                        question,
                        event.get("answer") or "",
                        event.get("tool_trace"),
                        event.get("usage"),
                        event.get("citations"),
                        event.get("views"),
                        owner=uid,
                    )
                    event["session"] = {
                        "id": session["id"],
                        "title": session["title"],
                        "created_at": session["created_at"],
                        "updated_at": session["updated_at"],
                        "message_count": session["message_count"],
                        "href": f"/api/sessions/{quote(session['id'])}",
                    }
                emit(event)
        except (llm.LlmAuthError, llm.LlmForbiddenError) as e:
            self._auto_disconnect_on_auth_failure(override)
            _status, body = _sanitize_probe_failure(e)
            try:
                emit({"type": "error", **body, "reconnect_required": True})
            except Exception:
                pass
        except llm.LlmRateLimitError as e:
            _status, body = _sanitize_probe_failure(e)
            try:
                emit({"type": "error", **body})
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            try:
                emit({"type": "error", "error": str(e)})
            except Exception:
                pass

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    mode = "MOCK (no model)" if config.LLM_MOCK else f"model={config.LLM_MODEL} @ {config.LLM_BASE_URL}"
    print(f"MDC assistant: http://{config.HOST}:{config.PORT}   [{mode}]")
    print(f"  single entry — proxying arch/impact/coverage + data from {config.RETRIEVAL_UPSTREAM}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
