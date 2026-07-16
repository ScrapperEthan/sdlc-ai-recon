"""Tool registry: OpenAI-style schemas + dispatch into the retrieval layer.
Add a tool by adding a schema here and a branch in dispatch()."""
import outage_report
from retriever import graph, messages as msg, code, flow, unified_impact, arch_focus

# Order affected-repo relations from the most direct (the vendor's own delivery/API) to the widest
# (dependency closure), so the inline view's repo sample leads with what actually breaks.
_REL_ORDER = {"delivery-job": 0, "outbound-api": 1, "channel-owner": 2, "msg-channel": 3,
              "serves-channel": 4, "dependency-downstream": 5, "dependency-upstream": 6}


def _arch_impact(kind, value):
    """Best-effort outage impact (use-cases + repos) for the inline arch view; {} if data absent."""
    try:
        report = outage_report.build_report(f"{kind}:{value}")
    except Exception:  # noqa: BLE001 — impact is a bonus; the diagram still renders without it
        return {}
    use_cases = report.get("affected_use_cases") or {}
    repos = report.get("affected_repos") or {}
    items = repos.get("items") or []
    sample = [item.get("repo") for item in
              sorted(items, key=lambda x: _REL_ORDER.get(x.get("relation"), 9))[:6] if item.get("repo")]
    return {
        "confidence": report.get("confidence"),
        "use_cases": {"count": use_cases.get("count", 0),
                      "items": [i.get("use_case") for i in (use_cases.get("items") or [])[:8]]},
        "repos": {"count": repos.get("count", 0), "by_relation": repos.get("by_relation") or {}, "sample": sample},
    }


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
    _schema("unified_impact",
            "CROSS-REPO CALL GRAPH + blast radius. For 'who calls / who uses / the call chain of X' "
            "(X = a class, method, service, or repo), pass X as `seed`. Returns REAL callers from the "
            "per-bundle CodeGraph index — auto-routed to the right bundle, you do NOT need to know it — "
            "plus dependency and async-message peers. PREFER THIS over search_code for any call/usage "
            "relationship; it returns precise cross-repo call paths, not text matches. Only fall back "
            "to search_code/read_file if the result's `callers.available` is false.",
            {"seed": {"type": "string"}, "transitive": {"type": "boolean"},
             "bundle": {"type": "string"}}, ["seed"]),
    _schema("call_graph",
            "Raw `codegraph explore <query>` for a symbol, routed to the bundle that defines it. "
            "Prefer `unified_impact` (it wraps this plus deps/messages); use this only for a raw dump.",
            {"query": {"type": "string"}}, ["query"]),
    _schema("show_arch",
            "Render the architecture diagram INLINE in your answer with the affected chain "
            "highlighted. Call this whenever the user asks what is affected / impacted / broken by a "
            "CHANNEL or VENDOR problem or outage (e.g. 'SMS channel is down', '短信受影响了', 'Sinch "
            "出问题了'). `kind` is 'channel' or 'vendor'; `value` is the channel (sms/email/push/mms/"
            "whatsapp/wechat/letter) or the vendor (sinch/csl/3hk/…). The user then SEES the "
            "highlighted diagram in your reply — they never open a page or click a node themselves. "
            "Still write the affected-path explanation in text too.",
            {"kind": {"type": "string"}, "value": {"type": "string"}}, ["kind", "value"]),
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
    if name == "unified_impact":
        return unified_impact.query(a["seed"], a.get("transitive", False), a.get("bundle"))
    if name == "call_graph":
        # Route to the bundle that defines the symbol, reusing the retriever's routed explorer
        # (the previous copy shelled `codegraph explore` in the process cwd — no index there).
        root = unified_impact.bundle_root_for(a["query"])
        return unified_impact._call_graph(a["query"], cwd=root)
    if name == "show_arch":
        result = arch_focus.focus(a.get("kind"), a.get("value"))
        if isinstance(result, dict) and result.get("ok"):
            impact = _arch_impact(result["kind"], result["value"])
            if impact:
                result["impact"] = impact
        return result
    return {"error": f"unknown tool: {name}"}
