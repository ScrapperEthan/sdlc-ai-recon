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

from . import agent, config, session_store, llm_routes, llm_credentials, llm
from retriever import code as rcode, config as rconfig

HERE = os.path.dirname(__file__)
INDEX = os.path.join(HERE, "static", "index.html")

# Generic, sanitized message for any connect/select/register probe failure. Deliberately never the
# raw exception str() -- a provider's own error can embed the upstream response body or a full
# endpoint URL (see e.g. copilot_responses.chat's "copilot-api unreachable at <url>: ..."), and
# those must never reach the client (spec: no Token/Authorization/proxy-credential/URL/upstream body).
_LLM_PROBE_ERROR = "could not verify that endpoint/model -- check the connection and try again"


def _pick_default_model(listing):
    """From an `llm.models()` result, the model to try first: the provider's own default, else the
    first listed model, else "" (let the provider fall back to its own configured default)."""
    listing = listing or {}
    default_model = (listing.get("default_model") or "").strip()
    if default_model:
        return default_model
    for entry in listing.get("models") or []:
        model_id = (entry.get("id") or "").strip()
        if model_id:
            return model_id
    return ""


def _probe_llm(override):
    """Bind `override` for one throwaway chat call to confirm the endpoint+model actually work.
    Raises on any failure; callers decide what to roll back. Never touches any store itself."""
    otoken = config.set_llm_override(override)
    try:
        llm.chat([{"role": "user", "content": "ping"}], tools=None, temperature=0)
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
        credential = llm_credentials.describe(user_token)
        if credential.get("connected"):
            return {"registered": True, "mode": "copilot_token", "label": "Copilot (token mode)",
                    "token_mode_available": True, "model": credential.get("selected_model") or ""}
        return {"registered": False, "mode": "shared", "token_mode_available": True,
                "model": config.llm_default_model()}

    def _resolve_llm_override(self, user_token):
        """Bind this request to the caller's own LLM endpoint/provider for the whole agent turn.

        Flag OFF: byte-for-byte the original behaviour -- a straight `llm_routes.resolve` call
        (tunnel override or None -> env default), no credential-store lookup at all. Flag ON: when
        the token isn't a registered tunnel, also checks the RAM-only paste-token credential store
        and -- if it holds a live credential for this token -- selects provider
        `github_copilot_direct` with that `credential_id`, plus its confirmed `selected_model` (set
        by the connect-token probe or a later POST /api/llm/select-model) and the credential's
        `owner_uid` (audit/ownership metadata carried alongside, same shape `llm_credentials`
        already tracks -- not consulted by config.py, which only resolves the known LLM_* keys),
        through the SAME `config.set_llm_override`/`reset_llm_override` path tunnel users already
        use (see config.py). An unknown/stale/disconnected credential_id resolves to None here, same
        as an unknown tunnel token -- i.e. it fails closed to the shared env-default LLM, never
        silently reuses someone else's endpoint."""
        override = llm_routes.resolve(user_token)
        if override is not None or not config.LLM_TOKEN_MODE_ENABLED:
            return override
        record = llm_credentials.resolve(user_token)
        if record is not None:
            override = {"mode": "copilot_token", "provider": "github_copilot_direct",
                        "credential_id": user_token, "owner_uid": record.get("owner_uid") or ""}
            model = record.get("selected_model")
            if model:
                override["model"] = model
                override["selected_model"] = model
            return override
        return None

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
            try:
                listing = _list_models(self._resolve_llm_override(self._user_token()))
            except Exception:  # noqa: BLE001 -- never relay the raw provider error (may embed a URL)
                self._send_json(502, {"error": _LLM_PROBE_ERROR})
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
                candidate = {"base_url": base_url}
                if model:
                    candidate["model"] = model
                if api_key:
                    candidate["api_key"] = api_key
                if not model:
                    try:
                        model = _pick_default_model(_list_models(candidate))
                    except Exception:  # noqa: BLE001 -- listing is best-effort; the probe below is what gates persistence
                        model = ""
                    if model:
                        candidate["model"] = model
                try:
                    _probe_llm(candidate)
                except Exception:  # noqa: BLE001 -- never relay the raw provider error
                    self._send_json(400, {"error": _LLM_PROBE_ERROR})
                    return
                try:
                    record = llm_routes.register(
                        base_url, model, api_key,
                        req.get("label") or "", req.get("provider") or "", req.get("token") or None,
                    )
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                else:
                    self._send_json(200, record)
                return

            if path == "/api/llm/connect-token":
                # Internal beta (SDLC_LLM_TOKEN_MODE): the tester pastes their own `.copilot_token`;
                # it lands ONLY in the RAM-only llm_credentials store (never a file, never logged) --
                # see webapp/llm_credentials.py. The browser then sends the returned credential_id
                # back as its X-SDLC-User-Token, same header/pairing-token mechanism tunnel mode
                # already uses (see _resolve_llm_override).
                #
                # Only report "Connected" once a REAL llm.chat() probe against a real model has
                # succeeded -- a token that merely looks non-blank (e.g. copy-pasted wrong, revoked,
                # no Copilot entitlement) must never be reported as connected. On any failure the
                # freshly-created credential is deleted, not left dangling in RAM.
                try:
                    credential_id = llm_credentials.connect(req.get("token"), owner_uid=self._uid)
                except ValueError as e:
                    self._send_json(400, {"error": str(e)})
                    return
                base_override = {"mode": "copilot_token", "provider": "github_copilot_direct",
                                  "credential_id": credential_id}
                try:
                    model = _pick_default_model(_list_models(base_override))
                    probe_override = dict(base_override)
                    if model:
                        probe_override["model"] = model
                        probe_override["selected_model"] = model
                    _probe_llm(probe_override)
                except Exception:  # noqa: BLE001 -- never relay the raw provider error
                    llm_credentials.disconnect(credential_id)
                    self._send_json(400, {"error": _LLM_PROBE_ERROR})
                    return
                if model:
                    llm_credentials.set_selected_model(credential_id, model, owner_uid=self._uid)
                self._send_json(200, {"credential_id": credential_id, "model": model, "connected": True})
                return

            if path == "/api/llm/disconnect-token":
                credential_id = (req.get("credential_id") or "").strip() or self._user_token()
                llm_credentials.disconnect(credential_id)
                self._send_json(200, {"ok": True})
                return

            if path == "/api/llm/select-model":
                # Switch the model for whichever endpoint this browser is currently bound to (tunnel
                # route or, in token mode, its credential). Re-confirms the model is still in the
                # provider's own list, then re-probes with a real llm.chat() call BEFORE persisting
                # anything -- a failed switch leaves the previously-confirmed model untouched.
                user_token = self._user_token()
                new_model = (req.get("model") or "").strip()
                if not new_model:
                    self._send_json(400, {"error": "model is required"})
                    return

                tunnel_override = llm_routes.resolve(user_token)
                credential_record = (llm_credentials.resolve(user_token)
                                     if tunnel_override is None and config.LLM_TOKEN_MODE_ENABLED else None)
                if tunnel_override is not None:
                    candidate = dict(tunnel_override, model=new_model, selected_model=new_model)
                elif credential_record is not None:
                    if (credential_record.get("owner_uid") or "") != self._uid:
                        self._send_json(403, {"error": "credential is not owned by this session"})
                        return
                    candidate = {"mode": "copilot_token", "provider": "github_copilot_direct",
                                 "credential_id": user_token,
                                 "owner_uid": credential_record.get("owner_uid") or "",
                                 "model": new_model, "selected_model": new_model}
                else:
                    self._send_json(400, {"error": "connect an LLM before choosing a model"})
                    return

                try:
                    listing = _list_models(candidate)
                except Exception:  # noqa: BLE001
                    self._send_json(502, {"error": _LLM_PROBE_ERROR})
                    return
                available_ids = {(m.get("id") or "").strip() for m in listing.get("models") or []}
                if new_model not in available_ids:
                    self._send_json(400, {"error": "model is no longer available; refresh the model list"})
                    return

                try:
                    _probe_llm(candidate)
                except Exception:  # noqa: BLE001 -- never relay the raw provider error; old model stays active
                    self._send_json(400, {"error": _LLM_PROBE_ERROR})
                    return

                if tunnel_override is not None:
                    llm_routes.register(
                        tunnel_override["base_url"], new_model, tunnel_override.get("api_key") or "",
                        "", tunnel_override.get("provider") or "", user_token,
                    )
                else:
                    llm_credentials.set_selected_model(user_token, new_model, owner_uid=self._uid)
                self._send_json(200, {"ok": True, "model": new_model})
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
                    self._send_chat_stream(session_id, question, history, self._uid)
                finally:
                    config.reset_llm_override(otoken)
                return

            otoken = config.set_llm_override(override)
            try:
                result = agent.answer(question, history)
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

    def _send_chat_stream(self, session_id, question, history, uid):
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
