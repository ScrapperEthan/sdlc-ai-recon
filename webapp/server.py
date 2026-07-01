#!/usr/bin/env python3
"""Stdlib web server: serves the chat UI and a POST /api/chat endpoint.

Run from the workspace root (where mirror/, recon_out/, index/ live):
    python -m webapp.server
    # test with no model first:
    LLM_MOCK=1 python -m webapp.server
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

from . import agent, config, session_store

HERE = os.path.dirname(__file__)
INDEX = os.path.join(HERE, "static", "index.html")


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
        elif path == "/api/sessions":
            self._send_json(200, {"sessions": session_store.list_sessions()})
        elif path.startswith("/api/sessions/"):
            session_id = unquote(path.removeprefix("/api/sessions/"))
            try:
                session = session_store.get_session(session_id)
            except KeyError:
                self._send_json(404, {"error": f"Session not found: {session_id}"})
            else:
                self._send_json(200, session)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/chat", "/api/sessions"):
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

            result = agent.answer(question, history)
            session = session_store.append_exchange(
                session_id,
                question,
                result.get("answer") or "",
                result.get("tool_trace"),
                result.get("usage"),
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

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    mode = "MOCK (no model)" if config.LLM_MOCK else f"model={config.LLM_MODEL} @ {config.LLM_BASE_URL}"
    print(f"HASE assistant: http://{config.HOST}:{config.PORT}   [{mode}]")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
