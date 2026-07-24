"""Tool registry: OpenAI-style schemas + dispatch into the retrieval layer.
Add a tool by adding a schema here and a branch in dispatch()."""
import impact_report
import outage_report
from retriever import (graph, messages as msg, code, flow, unified_impact, arch_focus,
                        usecase_consistency, usecase_master, repo_tags)

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
    ordered = [item.get("repo") for item in
               sorted(items, key=lambda x: _REL_ORDER.get(x.get("relation"), 9)) if item.get("repo")]
    uc_items = [i.get("use_case") for i in (use_cases.get("items") or []) if i.get("use_case")]
    # Carry the FULL affected chain (bounded) into the tool result, not just a 6-item teaser, so the
    # model can answer from what the diagram actually shows instead of re-searching the mirror.
    return {
        "confidence": report.get("confidence"),
        "use_cases": {"count": use_cases.get("count", 0), "items": uc_items[:40],
                      "truncated": len(uc_items) > 40},
        "repos": {"count": repos.get("count", 0), "by_relation": repos.get("by_relation") or {},
                  "items": ordered[:40], "truncated": len(ordered) > 40, "sample": ordered[:6]},
    }


def _schema(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


def _impact_view(repo):
    """Dependency blast-radius as an INLINE view dict (used by `show_impact` and `impact(inline=1)`).
    Downstream = repos that DEPEND ON this one and break if it changes (graph.impact's
    `depended_on_by`); upstream = this repo's own deps (`depends_on`). Keep these straight."""
    repo = (repo or "").strip()
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
    downstream = dep["depended_on_by"]  # affected consumers — the answer to "连累谁"
    upstream = dep["depends_on"]         # this repo's own dependencies
    return {
        "ok": True, "view": "impact", "url": url,
        "summary": f"已在依赖图上展开 {repo} 的影响：下游（受影响）{len(downstream)} 个、上游（依赖）{len(upstream)} 个仓库。",
        # Full (bounded) lists so the model sees the blast radius it just rendered, not only a sample.
        "downstream": {"count": len(downstream), "repos": sorted(downstream)[:60],
                       "truncated": len(downstream) > 60},
        "upstream": {"count": len(upstream), "repos": sorted(upstream)[:60],
                     "truncated": len(upstream) > 60},
        "impact": {"repos": {"count": len(downstream) + len(upstream),
                             "by_relation": {"dependency-downstream": len(downstream),
                                             "dependency-upstream": len(upstream)},
                             "sample": sorted(downstream)[:6]}},
    }


def _coverage_view(kind, value):
    """Estate overview as an INLINE view dict (used by `show_coverage` and `list_repos(inline=1)`).
    Also carries the actual repo list + count so the model isn't blind to the estate view it renders
    (the iframe fetches these itself; without them the model would re-grep the mirror to answer)."""
    kind = (kind or "").strip().lower()
    value = (value or "").strip()
    param = ("channel=" + value) if kind == "channel" and value else (("q=" + value) if value else "")
    view = {"ok": True, "view": "coverage",
            "url": "/coverage.html?embed=1" + ("&" + param if param else ""),
            "summary": ("仓库全景" + (f"（筛选：{kind}:{value}）" if value else "（全量）"))}
    try:
        if kind == "channel" and value:
            data = repo_tags.filter_repos(channel=value)
        elif value:
            data = repo_tags.filter_repos(query=value)
        else:
            data = repo_tags.filter_repos()
    except Exception:  # noqa: BLE001 — the diagram still renders without the list; data is a bonus
        return view
    repos = data.get("repos") or []
    view["count"] = data.get("count", len(repos))
    view["repos"] = repos[:60]
    view["repos_truncated"] = len(repos) > 60
    return view


TOOLS = [
    _schema("impact", "Dependency blast radius: who depends on a repo and what it depends on. "
            "Set inline=true to ALSO render the dependency graph inline in your answer (for "
            "'what breaks if I change X', '改 X 会连累谁', 'is X risky to touch').",
            {"repo": {"type": "string"}, "transitive": {"type": "boolean"},
             "inline": {"type": "boolean"}}, ["repo"]),
    _schema("hubs", "Most depended-on repos (riskiest to change).",
            {"top": {"type": "integer"}}),
    _schema("message_flow",
            "Async message wiring over the message-edge snapshot. Pick ONE entry point: pass "
            "`destination` (a topic/queue substring) to find repos on it — `direction` 'consume' "
            "(default) lists CONSUMERS, 'produce' lists PRODUCERS, 'both' returns both; OR pass "
            "`repo` to list every produce/consume edge touching that repo; OR pass `use_case_id` "
            "(optionally with `destination`) to STITCH the async path use-case -> topic -> "
            "consumers across sources. Use for '谁在消费/生产这个 topic', 'who produces to this "
            "queue', 'this repo's message routes', 'trace this use case's message flow'.",
            {"destination": {"type": "string"}, "direction": {"type": "string"},
             "repo": {"type": "string"}, "use_case_id": {"type": "string"}}),
    _schema("usecase_routing",
            "use-case <-> topic routing from the dev/SCT snapshot. FORWARD (default): pass "
            "`use_case_id` for its topic(s), OR `topic` to search use cases by topic substring, OR "
            "BOTH for exact PAIR verification (does this pair exist). REVERSE: set `reverse=true` "
            "with a `topic` to list EVERY use case routing to it — the answer to 'what other use "
            "cases share this topic', '这个 topic 还有哪些 use case', 'which use cases are affected "
            "if this topic/channel changes'. `exact` (default true; false=substring) and `limit` "
            "apply to the reverse lookup, which returns total/returned/truncated + snapshot "
            "provenance (never a cut-off blob). ALWAYS report the dev/SCT-vs-production caveat and "
            "never say 'no other use cases exist' when you mean 'none in this snapshot'.",
            {"use_case_id": {"type": "string"}, "topic": {"type": "string"},
             "reverse": {"type": "boolean"}, "exact": {"type": "boolean"},
             "limit": {"type": "integer"}}),
    _schema("list_repos",
            "REPO DIRECTORY lookup (not code search). Call this for 'what repos does X have', "
            "'what APIs does X expose', 'what tracking repos are there' — X being a repo-name "
            "family/keyword like 'mdc', 'sms', 'tracking'. `query` is a case-insensitive SUBSTRING "
            "match against repo names — pass the user's word as-is (e.g. query='mdc' matches the "
            "`amet-mdc-*` repo family). `mode` filters by role: 'api' (HTTP-facing ingress services — "
            "this is where REST endpoints live), 'job' (batch/scheduled), 'core' (business logic "
            "libraries pulled in by an api/job shell), or 'lib' (shared library). `channel` and "
            "`system` filter on the same tag dimensions. Recommended recipe for 'what APIs does MDC "
            "have': call `list_repos(query='mdc', mode='api')` FIRST to get the exact repo list, THEN "
            "`search_code` (scoped to those repos via its `repos` param) for `@PostMapping`/"
            "`@GetMapping` inside them — do NOT guess repo names by grepping the whole mirror. "
            "IMPORTANT NAME COLLISION: 'MDC' is ALSO an upstream business `source_system` (distinct "
            "from the `amet-mdc-*` repo family) that feeds ~880 use cases. This tool answers the "
            "REPO/code question ('what repos/APIs does MDC have', 'where does MDC code live'). If the "
            "question is instead 'MDC 出问题影响哪些 use case / 渠道' or 'MDC 改动要通知谁' (business-"
            "impact / who-to-notify), use `source_system_impact` instead, not this tool. "
            "For 'list the FULL MDC repo list / MDC 完整仓库清单 (including mc-hk-hase-*)', call "
            "`list_repos(group='mdc')` instead of `query`/`system` — it returns the UNION of the "
            "`amet-mdc-*` name family and the business-sheet `mdc_common` tag in one shot, with a hard "
            "`count` and a per-repo `via` (`amet-mdc-prefix` or `mdc_common`) showing why each repo is "
            "in the group. mc-hk-hase-* members only ever get in via `mdc_common` (the MDC business "
            "sheet's MDC-Common flag) — NEVER via the name, since they don't contain 'mdc' and their "
            "`system` tag is 'hase', not 'amet-mdc'. **Copy `count` verbatim into your answer — never "
            "count or subset the `repos` list yourself.** `mdc_common=true` (without `group`) filters "
            "the plain query/system/etc. path down to just the business-sheet-flagged repos. "
            "Set inline=true to ALSO render the estate overview inline (optionally filtered by "
            "`channel`, else `query`) — this replaces the old show_coverage tool.",
            {"query": {"type": "string"}, "mode": {"type": "string"},
             "channel": {"type": "string"}, "system": {"type": "string"},
             "group": {"type": "string"}, "mdc_common": {"type": "boolean"},
             "inline": {"type": "boolean"}}),
    _schema("search_code", "Search the read-only mirror; returns 'path:line:text'. Pass `repos` "
            "(a list of exact repo ids, e.g. from `list_repos`) to scope the search to just those "
            "repos instead of scanning the whole ~390-repo mirror — much faster and avoids noise "
            "from unrelated repos with similar code.",
            {"pattern": {"type": "string"}, "glob": {"type": "string"},
             "max_results": {"type": "integer"},
             "repos": {"type": "array", "items": {"type": "string"}}}, ["pattern"]),
    _schema("read_file", "Read line-numbered source (path relative to mirror/).",
            {"path": {"type": "string"}, "start": {"type": "integer"},
             "end": {"type": "integer"}}, ["path"]),
    _schema("unified_impact",
            "CROSS-REPO CALL GRAPH + blast radius. For 'who calls / who uses / the call chain of X' "
            "(X = a class, method, service, or repo), pass X as `seed`. Returns REAL callers from the "
            "per-bundle CodeGraph index — auto-routed to the right bundle, you do NOT need to know it — "
            "plus dependency and async-message peers. PREFER THIS over search_code for any call/usage "
            "relationship; it returns precise cross-repo call paths, not text matches. Only fall back "
            "to search_code/read_file if the result's `callers.available` is false.",
            {"seed": {"type": "string"}, "transitive": {"type": "boolean"},
             "bundle": {"type": "string"}}, ["seed"]),
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
    _schema("usecase_impact",
            "FULL detail for ONE Use Case: identity/business/governance, consent-preflight (policy "
            "checks, NOT the channel list), channels_declared (channel_rule.channel FACT), resolved "
            "endpoint_repos (declared source_system entrypoint), the rule_text decision expression "
            "as a STRUCTURAL tree (rule_text_ast — mode/operator_tree/normalized_expression; while "
            "semantics=='unconfirmed' — the default — NEVER read this as an asserted fallback/"
            "parallel order, only as structure), validation_findings (rule_text vs channel_rule "
            "consistency, severity-ranked), plus upstream/downstream repos and the channel chain. "
            "Call this for 'what is use case X', 'tell me about M2050', 'M2050 的渠道/上游/owner 是"
            "什么', or before answering any question about one specific use_case_id's configuration.",
            {"use_case_id": {"type": "string"}}, ["use_case_id"]),
    _schema("search_usecases",
            "Use Case CATALOG search across the full ~2,800-row master dataset, server-side "
            "paginated (never dumps the whole table — `items` defaults to the first 50 matches; "
            "use `offset`/`limit` to page). Filters: `query` (substring on id/name/project), "
            "`source_system` (canonicalized), `include_inactive`, `channel` (from channel_rule "
            "fact), `business_category_code`, `country`, `service_line`, `delivery_mode`. Call this "
            "for 'find use cases matching X', 'list SMS use cases in HK', 'show me PEGA batch use "
            "cases' — anything that filters/browses the catalog rather than asking about ONE known "
            "use_case_id (for that, use usecase_impact instead).",
            {"query": {"type": "string"}, "source_system": {"type": "string"},
             "include_inactive": {"type": "boolean"}, "channel": {"type": "string"},
             "business_category_code": {"type": "string"}, "country": {"type": "string"},
             "service_line": {"type": "string"}, "delivery_mode": {"type": "string"},
             "offset": {"type": "integer"}, "limit": {"type": "integer"}}, []),
    _schema("usecase_quality_findings",
            "Data-quality / consistency DASHBOARD across the whole active Use Case dataset: orphan "
            "channel_rule/ext rows, active use cases with no channel rule, master rows missing Ext, "
            "null priority (risk of NPE), PUSH+INBOX with no route/router, illegal business_category "
            "codes, plus PER-USE-CASE rule_text-vs-channel_rule mismatches (channel_set_mismatch, "
            "expression_vs_priority — e.g. the I0141/I0142 case where rule_text disagrees with the "
            "priority order). Severity-ranked; `counts_by_severity` is always the FULL breakdown, "
            "`findings` is paginated (`offset`/`limit`) and optionally filtered by `severity` "
            "('error'/'warning'/'info'). These are FLAGGED disagreements between data sources, not "
            "confirmed production failures — say so. Call for 'what data quality issues are there', "
            "'show me config problems', '有哪些 use case 配置有问题'.",
            {"severity": {"type": "string"}, "offset": {"type": "integer"},
             "limit": {"type": "integer"}}, []),
]


def dispatch(name, a):
    a = a or {}
    if name == "impact":
        if a.get("inline"):
            return _impact_view(a["repo"])
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
    if name == "message_flow":
        # Merges consumers/producers/repo_routes/trace: one entry point over the message-edge
        # snapshot, disambiguated by which arg is supplied.
        dest = (a.get("destination") or "").strip()
        repo = (a.get("repo") or "").strip()
        use_case = (a.get("use_case_id") or "").strip()
        if use_case:
            return flow.trace(use_case or None, dest or None)
        if repo:
            return msg.routes_for_repo(repo)
        if dest:
            direction = (a.get("direction") or "consume").strip().lower()
            if direction == "produce":
                return {"direction": "produce", "matches": msg.who_produces(dest)}
            if direction == "both":
                return {"direction": "both", "consumers": msg.who_consumes(dest),
                        "producers": msg.who_produces(dest)}
            return {"direction": "consume", "matches": msg.who_consumes(dest)}
        return {"error": "message_flow needs one of: destination, repo, or use_case_id"}
    if name == "usecase_routing":
        # Merges usecase_route (forward) + use_cases_for_topic (reverse=true).
        if a.get("reverse"):
            topic = (a.get("topic") or "").strip()
            if not topic:
                return {"error": "usecase_routing reverse=true needs a topic"}
            return msg.reverse_lookup_use_cases(topic, a.get("exact", True), a.get("limit", 50))
        return msg.usecase_route(a.get("use_case_id") or None, a.get("topic") or None)
    if name == "list_repos":
        if a.get("inline"):
            if a.get("channel"):
                return _coverage_view("channel", a.get("channel"))
            return _coverage_view("query", a.get("query"))
        try:
            group = (a.get("group") or "").strip().lower()
            if group:
                if group == "mdc":
                    return repo_tags.mdc_repos()
                # An unregistered group used to fall through to an unfiltered filter_repos and
                # SILENTLY return all 392 repos (RUNBOOK-48 D1). Reject it explicitly instead.
                return {"ok": False, "error": f"unknown group: {a.get('group')}",
                        "allowed_groups": ["mdc"],
                        "hint": "omit group and use query=<substring> to search repo names, "
                                "e.g. query='campaign'"}
            return repo_tags.filter_repos(
                channel=a.get("channel") or None,
                mode=a.get("mode") or None,
                system=a.get("system") or None,
                query=a.get("query") or None,
                mdc_common=a.get("mdc_common"),
            )
        except FileNotFoundError:
            return {"ok": False, "error": "repo_tags.json not built; run make_repo_tags.py"}
    if name == "search_code":
        return code.search_code(a["pattern"], a.get("glob", "*.java"), a.get("max_results", 50),
                                 a.get("repos"))
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
        return _impact_view(a.get("repo"))
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
    if name == "usecase_impact":
        value = (a.get("use_case_id") or "").strip()
        if not value:
            return {"ok": False, "error": "use_case_id is required"}
        try:
            return impact_report.build_report(f"use-case:{value}")
        except (FileNotFoundError, ValueError) as error:
            return {"ok": False, "error": str(error)}
    if name == "search_usecases":
        return usecase_master.search_usecases(
            query=a.get("query") or None,
            source_system=a.get("source_system") or None,
            include_inactive=bool(a.get("include_inactive", False)),
            channel=a.get("channel") or None,
            business_category_code=a.get("business_category_code") or None,
            country=a.get("country") or None,
            service_line=a.get("service_line") or None,
            delivery_mode=a.get("delivery_mode") or None,
            offset=int(a.get("offset") or 0),
            limit=int(a.get("limit") or 50),
        )
    if name == "usecase_quality_findings":
        return usecase_consistency.quality_findings(
            severity=a.get("severity") or None,
            offset=int(a.get("offset") or 0),
            limit=int(a.get("limit") or 50),
        )
    if name == "show_coverage":
        return _coverage_view(a.get("kind"), a.get("value"))
    return {"error": f"unknown tool: {name}"}
