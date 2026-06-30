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

from . import agent, config

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

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/chat":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
            result = agent.answer(req.get("question", ""), req.get("history"))
            self._send(200, json.dumps(result, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

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
