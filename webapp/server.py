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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import agent, config, session_store
from retriever import code as rcode, config as rconfig

HERE = os.path.dirname(__file__)
INDEX = os.path.join(HERE, "static", "index.html")


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
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) or b"{}"
        return json.loads(raw_body)

    def do_GET(self):
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
            self._send_json(200, {"sessions": session_store.list_sessions()})
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
        elif path.startswith("/api/sessions/"):
            session_id = unquote(path.removeprefix("/api/sessions/"))
            try:
                session = session_store.get_session(session_id)
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
        path = urlparse(self.path).path
        if path not in ("/api/chat", "/api/chat/stream", "/api/sessions"):
            self._send(404, b"not found", "text/plain")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/sessions":
                session = session_store.create_session(req.get("title") or "New session")
                self._send_json(201, session)
                return

            question = (req.get("question") or "").strip()
            if not question:
                self._send_json(400, {"error": "Question is required"})
                return

            session_id = req.get("session_id")
            if session_id:
                try:
                    history = session_store.history_for_agent(session_id)
                except KeyError:
                    self._send_json(404, {"error": f"Session not found: {session_id}"})
                    return
            else:
                session_id = session_store.create_session()["id"]
                history = []

            if path == "/api/chat/stream":
                self._send_chat_stream(session_id, question, history)
                return

            result = agent.answer(question, history)
            session = session_store.append_exchange(
                session_id,
                question,
                result.get("answer") or "",
                result.get("tool_trace"),
                result.get("usage"),
                result.get("citations"),
                result.get("views"),
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

    def _send_chat_stream(self, session_id, question, history):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
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
    print(f"HASE assistant: http://{config.HOST}:{config.PORT}   [{mode}]")
    print(f"  single entry — proxying arch/impact/coverage + data from {config.RETRIEVAL_UPSTREAM}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
