"""Tool registry: OpenAI-style schemas + dispatch into the retrieval layer.
Add a tool by adding a schema here and a branch in dispatch()."""
import shutil
import subprocess
from retriever import graph, messages as msg, code, flow


def _schema(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


TOOLS = [
    _schema("impact", "Dependency blast radius: who depends on a repo and what it depends on.",
            {"repo": {"type": "string"}, "transitive": {"type": "boolean"}}, ["repo"]),
    _schema("hubs", "Most depended-on repos (riskiest to change).",
            {"top": {"type": "integer"}}),
    _schema("consumers", "Repos that CONSUME a queue/topic (substring match).",
            {"destination": {"type": "string"}}, ["destination"]),
    _schema("producers", "Repos that PRODUCE to a queue/topic (substring match).",
            {"destination": {"type": "string"}}, ["destination"]),
    _schema("repo_routes", "All message edges (produce/consume) touching a repo.",
            {"repo": {"type": "string"}}, ["repo"]),
    _schema("usecase_route", "use-case -> topic from the dev/SCT routing snapshot.",
            {"use_case_id": {"type": "string"}, "topic": {"type": "string"}}),
    _schema("search_code", "Search the read-only mirror; returns 'path:line:text'.",
            {"pattern": {"type": "string"}, "glob": {"type": "string"},
             "max_results": {"type": "integer"}}, ["pattern"]),
    _schema("read_file", "Read line-numbered source (path relative to mirror/).",
            {"path": {"type": "string"}, "start": {"type": "integer"},
             "end": {"type": "integer"}}, ["path"]),
    _schema("trace", "Stitch use-case/destination across the async message wiring.",
            {"use_case_id": {"type": "string"}, "destination": {"type": "string"}}),
    _schema("call_graph", "Synchronous who-calls-whom via the CodeGraph CLI (if installed).",
            {"query": {"type": "string"}}, ["query"]),
]


def dispatch(name, a):
    a = a or {}
    if name == "impact":
        return graph.impact(a["repo"], a.get("transitive", False))
    if name == "hubs":
        return graph.hubs(a.get("top", 20))
    if name == "consumers":
        return msg.who_consumes(a["destination"])
    if name == "producers":
        return msg.who_produces(a["destination"])
    if name == "repo_routes":
        return msg.routes_for_repo(a["repo"])
    if name == "usecase_route":
        return msg.usecase_route(a.get("use_case_id") or None, a.get("topic") or None)
    if name == "search_code":
        return code.search_code(a["pattern"], a.get("glob", "*.java"), a.get("max_results", 50))
    if name == "read_file":
        return code.read_file(a["path"], a.get("start", 1), a.get("end"))
    if name == "trace":
        return flow.trace(a.get("use_case_id") or None, a.get("destination") or None)
    if name == "call_graph":
        return _call_graph(a["query"])
    return {"error": f"unknown tool: {name}"}


def _call_graph(query):
    cg = shutil.which("codegraph")
    if not cg:
        return {"available": False,
                "note": "codegraph CLI not on PATH; use search_code/read_file instead"}
    try:
        out = subprocess.run([cg, "explore", query], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        return {"available": True, "output": out.stdout[:8000]}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)}
