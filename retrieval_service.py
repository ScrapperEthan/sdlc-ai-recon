#!/usr/bin/env python3
"""Read-only HTTP wrapper around the retrieval layer."""
import impact_report
import outage_report
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from retriever import code, flow, graph, messages, glossary, repo_tags, usecase_master, config as rconfig

RETRIEVAL_HOST = os.environ.get("RETRIEVAL_HOST", "127.0.0.1")
RETRIEVAL_PORT = int(os.environ.get("RETRIEVAL_PORT", "8848"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
IMPACT_HTML_PATH = os.path.join(STATIC_DIR, "impact.html")
ARCH_HTML_PATH = os.path.join(STATIC_DIR, "arch.html")
ARCH_NODES_PATH = os.path.join(STATIC_DIR, "arch_nodes.json")
COVERAGE_HTML_PATH = os.path.join(STATIC_DIR, "coverage.html")


def _int(qs, key, default):
    try:
        return int((qs.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default


def _str(qs, key, default=""):
    return (qs.get(key) or [default])[0]


def _required(value, field):
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _required_repo(qs):
    repo = _required(_str(qs, "repo"), "repo")
    if repo not in graph.known_repos():
        raise FileNotFoundError(f"unknown repo: {repo}")
    return repo


def _health_payload():
    status_path = os.path.join(rconfig.INDEX_DIR, "last_indexed.json")
    try:
        with open(status_path, encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        indexed_as_of = None
    else:
        indexed_as_of = None
        if isinstance(payload, dict):
            indexed_as_of = payload.get("indexed_as_of") or payload.get("generated_at")
    return {"ok": True, "indexed_as_of": indexed_as_of}


def _repomap_text():
    with open(os.path.join(rconfig.INDEX_DIR, "REPOMAP.md"), encoding="utf-8-sig") as handle:
        text = handle.read()
    lines = []
    for line in text.splitlines():
        lines.append(line)
        if line.startswith("## "):
            repo = line[3:].strip()
            expanded = glossary.expand(repo)
            if expanded != repo:
                lines.append(f"- Name meaning: {expanded}")
    return "\n".join(lines).rstrip() + "\n"


def _impact_report_payload(qs):
    return impact_report.build_report(_required(_str(qs, "target"), "target"))


def _repos_payload(qs):
    return repo_tags.filter_repos(
        channel=_str(qs, "channel"),
        mode=_str(qs, "mode"),
        system=_str(qs, "system"),
        bundle=_str(qs, "bundle"),
    )


def _source_system_impact_payload(qs):
    value = _required(_str(qs, "source_system"), "source_system")
    return impact_report.build_report(f"source-system:{value}")


def _source_systems_payload():
    items = usecase_master.source_systems()
    return {"items": items, "count": len(items)}


def _outage_impact_payload(qs):
    supplied = [(key, _str(qs, key).strip()) for key in ("channel", "vendor", "repo") if _str(qs, key).strip()]
    if len(supplied) != 1:
        raise ValueError("exactly one of channel, vendor, or repo is required")
    kind, value = supplied[0]
    return outage_report.build_report(f"{kind}:{value}")


def _impact_page_body():
    with open(IMPACT_HTML_PATH, encoding="utf-8") as handle:
        return handle.read()


def _static_file(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _arch_map_payload():
    """Return the generated node->repo map, or an empty map plus a clear note if absent."""
    try:
        with open(rconfig.ARCH_MAP_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError):
        return {"nodes": {}, "note": "arch_map.json not generated yet; run make_arch_map.py (or python refresh.py)."}
    except json.JSONDecodeError:
        return {"nodes": {}, "note": "arch_map.json is invalid JSON; re-run make_arch_map.py."}
    if not isinstance(data, dict):
        return {"nodes": {}, "note": "arch_map.json is not an object; re-run make_arch_map.py."}
    return data


def _repo_tags_payload():
    """Return every repo's tags (the full 392-repo universe) for the coverage view, or an empty
    map plus a clear note if absent. Like /arch-map: read-only over a generated artifact."""
    try:
        with open(rconfig.REPO_TAGS_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError):
        return {"repos": {}, "note": "repo_tags.json not generated yet; run python refresh.py."}
    except json.JSONDecodeError:
        return {"repos": {}, "note": "repo_tags.json is invalid JSON; re-run make_repo_tags.py."}
    if not isinstance(data, dict):
        return {"repos": {}, "note": "repo_tags.json is not an object; re-run make_repo_tags.py."}
    return {"repos": data, "count": len(data)}


ROUTES = {
    "/impact": lambda qs: graph.impact(_required_repo(qs), _str(qs, "transitive").lower() in {"1", "true"}),
    "/hubs": lambda qs: graph.hubs(_int(qs, "top", 20)),
    "/consumers": lambda qs: messages.who_consumes(_required(_str(qs, "destination"), "destination")),
    "/producers": lambda qs: messages.who_produces(_required(_str(qs, "destination"), "destination")),
    "/repo-routes": lambda qs: messages.routes_for_repo(_required_repo(qs)),
    "/usecase": lambda qs: messages.usecase_route(_str(qs, "use_case_id") or None, _str(qs, "topic") or None),
    "/search": lambda qs: {
        "results": code.search_code(
            _required(_str(qs, "pattern"), "pattern"),
            _str(qs, "glob", "*.java"),
            _int(qs, "max", 50),
        )
    },
    "/read": lambda qs: {
        "path": _required(_str(qs, "path"), "path"),
        "text": code.read_file(
            _required(_str(qs, "path"), "path"),
            _int(qs, "start", 1),
            _int(qs, "end", 0) or None,
        ),
    },
    "/trace": lambda qs: flow.trace(_str(qs, "use_case_id") or None, _str(qs, "destination") or None),
    "/impact-report": _impact_report_payload,
    "/repos": _repos_payload,
    "/outage-impact": _outage_impact_payload,
    "/source-system-impact": _source_system_impact_payload,
    "/source-systems": lambda qs: _source_systems_payload(),
}


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

    def _method_not_allowed(self):
        self._send_json(405, {"error": "method not allowed"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path in {"/", "/static", "/static/", "/static/impact.html", "/impact.html"}:
                self._send(200, _impact_page_body(), "text/html; charset=utf-8")
                return
            if path in {"/arch.html", "/static/arch.html"}:
                self._send(200, _static_file(ARCH_HTML_PATH), "text/html; charset=utf-8")
                return
            if path in {"/coverage.html", "/static/coverage.html"}:
                self._send(200, _static_file(COVERAGE_HTML_PATH), "text/html; charset=utf-8")
                return
            if path == "/repo-tags":
                self._send_json(200, _repo_tags_payload())
                return
            if path in {"/arch_nodes.json", "/static/arch_nodes.json"}:
                self._send(200, _static_file(ARCH_NODES_PATH), "application/json; charset=utf-8")
                return
            if path == "/arch-map":
                self._send_json(200, _arch_map_payload())
                return
            if path == "/health":
                self._send_json(200, _health_payload())
                return
            if path == "/repomap":
                self._send(200, _repomap_text(), "text/plain; charset=utf-8")
                return
            if path not in ROUTES:
                self._send_json(404, {"error": f"unknown route: {path}"})
                return
            self._send_json(200, ROUTES[path](qs))
        except FileNotFoundError as error:
            self._send_json(404, {"error": str(error)})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            self._send_json(500, {"error": str(error)})

    def do_POST(self):
        self._method_not_allowed()

    def do_PUT(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def do_HEAD(self):
        self._method_not_allowed()

    def log_message(self, *args):
        pass


def create_server(host=None, port=None):
    bind_host = RETRIEVAL_HOST if host is None else host
    bind_port = RETRIEVAL_PORT if port is None else port
    return ThreadingHTTPServer((bind_host, bind_port), Handler)


def main():
    server = create_server()
    host, port = server.server_address[:2]
    print(f"Retrieval service: http://{host}:{port}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
