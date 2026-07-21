"""Tool registry: OpenAI-style schemas + dispatch into the retrieval layer.
Add a tool by adding a schema here and a branch in dispatch()."""
import impact_report
import outage_report
from retriever import graph, messages as msg, code, flow, unified_impact, arch_focus, usecase_master

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
    _schema("usecase_route",
            "use-case <-> topic from the dev/SCT routing snapshot. Pass ONLY use_case_id to get that "
            "use case's topic(s); pass ONLY topic to search use cases by topic (substring). Passing "
            "BOTH is PAIR VERIFICATION (does this exact pair exist) and does NOT list a topic's other "
            "use cases — for that, use use_cases_for_topic.",
            {"use_case_id": {"type": "string"}, "topic": {"type": "string"}}),
    _schema("use_cases_for_topic",
            "REVERSE lookup: given a TOPIC, list EVERY use case that routes to it (dev/SCT snapshot). "
            "Use this for 'what other use cases share this topic', 'which use cases are affected if "
            "this topic/channel changes', '这个 topic 还有哪些 use case', or after finding one use "
            "case's topic to see its siblings. Pass the FULL topic with exact=true (default) for a "
            "known topic; exact=false for a substring probe. Do NOT also pass a use_case_id — that "
            "hides the siblings. Returns total/returned/truncated (never a cut-off blob) + snapshot "
            "provenance; report the dev/SCT-vs-production caveat and never say 'no other use cases "
            "exist' when you mean 'none in this snapshot'.",
            {"topic": {"type": "string"}, "exact": {"type": "boolean"}, "limit": {"type": "integer"}},
            ["topic"]),
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
            "出问题了'), OR wants to see WHERE a business-upstream system or use case enters the "
            "pipeline (e.g. 'PEGA 接进来的位置在哪', '这个 Use Case 的上游系统在架构图上是哪个'). "
            "`kind` is 'channel', 'vendor', 'source-system', or 'use-case'; `value` is the channel "
            "(sms/email/push/mms/whatsapp/wechat/letter), the vendor (sinch/csl/3hk/…), the upstream "
            "system name (source-system), or a use_case_id (use-case, resolved to its declared "
            "source_system). The user then SEES the highlighted diagram in your reply — they never "
            "open a page or click a node themselves. Still write the affected-path explanation in "
            "text too.",
            {"kind": {"type": "string"}, "value": {"type": "string"}}, ["kind", "value"]),
    _schema("show_impact",
            "Render the dependency blast-radius INLINE in your answer for a repo the user wants to change "
            "or is worried about. Call this whenever the user asks 'what breaks if I change X', 'who depends "
            "on X', 'is X risky to touch', '改 X 会连累谁' (X = a repo name). `repo` is the repo id. The user "
            "SEES the impact inline; also summarise the downstream/upstream counts in text.",
            {"repo": {"type": "string"}}, ["repo"]),
    _schema("source_system_impact",
            "Business-upstream blast radius for an upstream system (PEGA/MDC/eAlert/L400/…): which "
            "Use Cases it feeds, the Round A coverage funnel (configured/expression_ready/"
            "entrypoint_traceable/catalog_only — STAGES, never claim 'reaches the customer'), the "
            "channel chain, upstream/downstream repos, and the layered OWNERS to notify on change "
            "(business_owners > cost_governance > config_maintainers). Call this for "
            "'PEGA/上游系统 出问题会影响哪些 Use Case / 渠道 / repo', 'L400 接入了哪些流程', or "
            "'改这个上游系统要通知谁'. `source_system` is the upstream system name (canonicalized; "
            "aliases folded via source_system_aliases.json if configured). Disabled use cases are "
            "excluded unless `include_inactive` is set. The `items` list defaults to the first 50 "
            "members (MDC ≈ 880 would otherwise overflow context) — use `offset`/`limit` to page "
            "through the rest; the coverage funnel counts are always the FULL total, never truncated.",
            {"source_system": {"type": "string"}, "include_inactive": {"type": "boolean"},
             "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["source_system"]),
    _schema("list_source_systems",
            "List distinct CANONICALIZED upstream business systems (source_system) — folds case/"
            "format variants (eAlert/e-Alert/…) into one entry with raw_variants listed — plus "
            "use-case/active/inactive counts. The picker for source_system_impact. Call when the "
            "user asks 'what upstream systems are there' or needs to disambiguate a name.",
            {}),
    _schema("show_coverage",
            "Render the 392-repo estate overview INLINE, optionally filtered. Call this when the user asks "
            "to see the repos on a channel or matching a keyword ('show me the SMS repos', '有哪些 tracking "
            "仓库', 'what does the estate look like'). `kind` is 'channel' or 'query'; `value` is the channel "
            "(sms/email/…) or a search keyword.",
            {"kind": {"type": "string"}, "value": {"type": "string"}}, ["kind"]),
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
    if name == "use_cases_for_topic":
        return msg.reverse_lookup_use_cases(a["topic"], a.get("exact", True), a.get("limit", 50))
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
        return unified_impact.call_graph(a["query"])
    if name == "show_arch":
        result = arch_focus.focus(a.get("kind"), a.get("value"))
        if isinstance(result, dict) and result.get("ok"):
            impact = _arch_impact(result["kind"], result["value"])
            if impact:
                result["impact"] = impact
        return result
    if name == "show_impact":
        repo = (a.get("repo") or "").strip()
        if not repo:
            return {"ok": False, "error": f"unknown repo: {repo}", "hint": "use an exact repo id"}
        try:
            known = graph.known_repos()
        except Exception:  # noqa: BLE001 — the embeddable page can still explain a missing index
            known = set()
        if known and repo not in known:
            return {"ok": False, "error": f"unknown repo: {repo}", "hint": "use an exact repo id"}
        url = f"/impact.html?embed=1&target={repo}"
        if not known:
            return {"ok": True, "view": "impact", "url": url,
                    "summary": f"已打开 {repo} 的依赖影响视图；当前依赖索引不可用，图中会显示可用的最佳结果。"}
        try:
            dep = graph.impact(repo, transitive=True)
        except Exception:  # noqa: BLE001 — impact chips are optional; keep the inline view usable
            return {"ok": True, "view": "impact", "url": url,
                    "summary": f"已打开 {repo} 的依赖影响视图；依赖计数暂不可用。"}
        # "改 X 会连累谁" asks for the blast radius: repos that DEPEND ON this one and break if it
        # changes = graph.impact's `depended_on_by` (downstream consumers). `depends_on` is what this
        # repo itself needs (its upstream deps). Keep these straight — the labels were inverted before.
        downstream = dep["depended_on_by"]  # affected consumers — the answer to "连累谁"
        upstream = dep["depends_on"]         # this repo's own dependencies
        return {
            "ok": True, "view": "impact",
            "url": url,
            "summary": f"已在依赖图上展开 {repo} 的影响：下游（受影响）{len(downstream)} 个、上游（依赖）{len(upstream)} 个仓库。",
            "impact": {"repos": {"count": len(downstream) + len(upstream),
                                    "by_relation": {"dependency-downstream": len(downstream), "dependency-upstream": len(upstream)},
                                    "sample": sorted(downstream)[:6]}},
        }
    if name == "source_system_impact":
        value = (a.get("source_system") or "").strip()
        if not value:
            return {"ok": False, "error": "source_system is required"}
        try:
            return impact_report.build_report(
                f"source-system:{value}",
                include_inactive=bool(a.get("include_inactive", False)),
                offset=int(a.get("offset") or 0),
                limit=int(a.get("limit") or 50),
            )
        except (FileNotFoundError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if name == "list_source_systems":
        return {"items": usecase_master.source_systems()}
    if name == "show_coverage":
        kind = (a.get("kind") or "").strip().lower()
        value = (a.get("value") or "").strip()
        param = ("channel=" + value) if kind == "channel" and value else (("q=" + value) if value else "")
        return {"ok": True, "view": "coverage",
                "url": "/coverage.html?embed=1" + ("&" + param if param else ""),
                "summary": ("仓库全景" + (f"（筛选：{kind}:{value}）" if value else "（全量 392）"))}
    return {"error": f"unknown tool: {name}"}
