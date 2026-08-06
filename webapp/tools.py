"""Tool registry: OpenAI-style schemas + dispatch into the retrieval layer.
Add a tool by adding a schema here and a branch in dispatch()."""
import impact_report
import outage_report
from retriever import (graph, messages as msg, code, flow, unified_impact, arch_focus,
                        usecase_consistency, usecase_master, repo_tags, incident, criticality)

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


def _channel_block(repo, downstream):
    """The tiered channel payload the inline view renders: conclusion, why, and the citations.

    Two separate questions, kept separate on the wire because collapsing them is what made the old
    answer read as a claim: `own` is what THIS repo handles (with the evidence that says so), and
    `downstream` is the spread across the repos that depend on it — affected, not owners.

    Best-effort throughout: a missing evidence file, an absent repo_tags or an unreadable scope file
    each degrade to "we cannot say", never to a smaller-looking number. Returns {} only when nothing
    at all could be computed, which the frontend renders as absence rather than as "no channels".
    """
    try:
        from retriever import channel_evidence
        tags = repo_tags.load()
        evidence = channel_evidence.load()
        scope = channel_evidence.load_scope()
        own = channel_evidence.for_repo(repo, tags=tags, evidence=evidence)
        spread = channel_evidence.aggregate(downstream or (), tags=tags, evidence=evidence,
                                            scope=scope)
    except Exception:  # noqa: BLE001 — the diagram and the counts must survive this being absent
        return {}
    return {
        "own": own,
        **channel_evidence.split_channels(own),
        "downstream": spread,
        # Provenance travels with the numbers. "There is evidence" and "the evidence covers this
        # repo" are different claims, and a UI that cannot tell them apart will imply the stronger.
        "evidence_available": bool(evidence.get("readable")),
        "evidence_records": evidence.get("records_loaded", 0),
        "scope_known": bool(scope.get("known")),
        "scanned_count": scope.get("scanned_count", 0),
        "unresolved_uncitable": scope.get("unresolved_count", 0),
    }


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
        # Rides the existing inline-view channel rather than opening a second one: the frontend
        # already receives this dict, and "which channels does this affect" is the question the
        # dependency picture is usually being asked in service of.
        "channels": _channel_block(repo, downstream),
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
            "source_system AND, when it has channel rules, highlighted all the way through topics/"
            "delivery jobs/outbound APIs to the carrier terminal). The user then SEES the "
            "highlighted diagram in your reply — they never open a page or click a node themselves. "
            "Still write the affected-path explanation in text too.",
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
            "`delivery_chain` is the FULL last mile — topics -> delivery jobs -> outbound APIs -> "
            "CARRIER -> what the customer actually receives (CSL/3HK SMSC, APNs/FCM, ProofPoint, "
            "print/mail) — with a `path_summary` line per channel; quote it for 全链路 / 出口 / 厂商 "
            "/ 最终发送 questions, and obey `vendor_selection.method`, strongest first: "
            "`router_table` = authoritative (tbl_use_case_router matched on its 4-column key AND "
            "the vendor name is owner-confirmed); `router_table_unconfirmed_alias` = the "
            "authoritative row DOES name a carrier — quote `authoritative_vendor_raw` VERBATIM (e.g. "
            "`HTCL`, `AWS HK SNS`) and say the canonical mapping is unconfirmed, do NOT translate it; "
            "`route_hint` = a route/router value names one, still a hint, cite the rule row; "
            "`channel_upper_bound` = 'at most these carriers', never 'it sends via X'. The "
            "authoritative table answers for only ~a quarter of rows and covers push/SMS only, so "
            "the upper bound is the normal case, not a defect. A vendor value also carries "
            "`lifecycle`: `legacy` (e.g. `HTCL OLD`) must be reported as a legacy route and NEVER "
            "merged with the unmarked value, and `provider_family`/`region` let you say 'AWS SNS, "
            "Hong Kong region' — a regional outage counts per region, a family-wide one merges them. "
            "SLA numbers are MILLISECONDS (inferred from the product code, not owner-signed-off): "
            "give both, e.g. '60000 ms (1 minute)', and say the unit comes from code. `delivery_path` "
            "now RESOLVES: `delivery_path_resolved` gives the name plus `system` (MDC/HASE/Shared) "
            "and `tier` (Time Critical / Real Time Express / Real Time Standard / Batch), e.g. "
            "'delivery_path 3 = HASE Real Time Standard (MDC)'. It is a CLASSIFICATION, not a route: "
            "say a message is classified on that path, never that it travels via it. If "
            "`known:false`, report the bare number — do NOT reuse a 'Path' phrase from an alert "
            "title, that is a different vocabulary. "
            "`use_case_master` says whether master data was "
            "found and, when not, WHY (dataset not configured vs id absent from that snapshot) — "
            "read it before writing 无法确认, so you name which half is missing instead of implying "
            "the use case has no channels or no owner. "
            "Call this for 'what is use case X', 'tell me about M2050', 'M2050 的渠道/上游/owner 是"
            "什么', 'M2050 的完整配置与渠道', or before answering any question about one specific "
            "use_case_id's configuration.",
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
    _schema("incident_impact",
            "INCIDENT triage: paste a raw production ALERT / alarm string and get who is affected. "
            "Call this whenever the user pastes something that looks like an alert — "
            "'prodECS_<repo>_service_CPUUtilizationMINOR[80percent]', 'MDC Alert - General SHP API "
            "Error - <repo>', 'MDC Error Counts Alert ... Delivery Job - <某条 Path>' — or asks "
            "'这个告警影响了谁', 'who does this incident affect', '出事了要通知谁', 'what business "
            "breaks if this service is down'. Pass the alert text VERBATIM in `alert_text`; do not "
            "clean it up, the repo id is usually embedded in it. Returns: the repos it identified "
            "(and WHY — `confirmed` = the exact repo id was present in the text, `candidate` = a "
            "hand-asserted resource mapping, say which), their message topics, the business use "
            "cases routed onto those topics with snapshot citations, and the channels. "
            "There are TWO ways in: a repo id embedded in the text, and a use-case id (e.g. "
            "`useCase=[M2101]`) — the largest alert family names no repo at all, so paste the "
            "colleagues' analysis output too if you have it, the use case in it is the way in. "
            "Repos found via a use case are `candidate` (they share a topic = involved in the "
            "path), NOT proof that repo failed — word it that way. "
            "IMPORTANT — report these honestly: (1) this reads NOTHING from production — no logs, "
            "no AWS, no MCP — so never present it as root cause, only as blast radius; "
            "(2) ALWAYS read `use_case_link` before quoting ANY use-case number: `available:false` "
            "= the precise join could NOT be computed (no same-environment route table); `matched:0` "
            "= the route snapshot does not cover this repo's topics. NEITHER means 'no business "
            "impact' — fall back to `channel_upper_bound` and word it as a ceiling ('at most N'). "
            "Say 'verify against production' before anyone tells a business team traffic stopped; "
            "(3) if `ok` is false the repo AND the use case "
            "could NOT be identified — say so and ask which service, NEVER guess; (4) `vendor` "
            "here is still null — `tbl_use_case_router` IS ingested now, but reaching it needs a "
            "use case, and this tool starts from an alert. For a carrier, go through "
            "`usecase_impact`; do NOT infer one from repo names; (5) a `delivery_path.phrase` here is "
            "a phrase from the ALERT TITLE and stays verbatim and unresolved. It is a different "
            "vocabulary from `tbl_use_case_router.delivery_path`, whose code table IS now known "
            "(1=Time Critical (MDC) ... 9=Shared Batch) — never map an alert phrase onto those "
            "numbers, that join would be invented.",
            {"alert_text": {"type": "string"}, "max_use_cases": {"type": "integer"}},
            ["alert_text"]),
    _schema("critical_repos",
            "Which repos are most critical — scored on SEVERAL independent axes, not one ranking. "
            "Call for '最核心的服务是哪些', 'top 10 core components', '哪个仓库最不能出问题', "
            "'最该加测试的是哪些'. "
            "IMPORTANT — how to report it: (1) the PER-AXIS ranks are the real answer; `score` is "
            "one equal-weighted view. A build-time dependency hub (`api-common`: everything depends "
            "on it, carries no messages) and a runtime traffic hub (a delivery job: carries every "
            "SMS, nothing depends on it) are different kinds of critical, and a single list hides "
            "which one you picked — so compare axes explicitly; (2) ALWAYS state "
            "`missing_dimensions` — production traffic, incident history and test coverage are NOT "
            "scored, so this is a top-10 of what is measurable today and must be presented that "
            "way, with who each missing axis is blocked on; (3) a `business` value of 0 means the "
            "dev/SCT route snapshot does not cover that repo's topics, NEVER 'no business impact'.",
            {"top": {"type": "integer"}}, []),
    _schema("incident_investigate",
            "ROOT-CAUSE track: read the actual PRODUCTION LOGS and CloudWatch METRICS for an alert. "
            "This is the other half "
            "of incident response — `incident_impact` answers 'who is affected' with zero "
            "production access, this one answers 'what does the log say'. Call it when the user "
            "asks '看日志', 'what does the log show', '为什么挂的', 'root cause', or after "
            "incident_impact when they want to know WHY rather than WHO. "
            "THREE INDEPENDENT BRANCHES run and are reported separately, in ascending order of what "
            "they need to know: PORTAL delivery records (needs only a `tracking_id` — no repo, no "
            "alarm, no time window), the CloudWatch metric around the alarm (needs an alarm name "
            "plus a time window), and LogDream logs (needs the repo/app plus a time window). "
            "PORTAL is the ONLY path for the `MDC Alert - General SHP API Error` family, which "
            "carries no alarm name and no resolvable app — if the alert or the user gives you a "
            "tracking id, pass it. `record_found: false` is a QUERY RESULT, not proof of delivery "
            "and not proof of no impact. `portal_channel` defaults to auto (tries SMS then Email). "
            "The tracking id is fingerprinted at the exit, so you will see `<tracking:...>` and "
            "never the real value — offer the user the id THEY gave you, do not invent one. "
            "Any branch can succeed while the others refuse — read "
            "`plan.cloudwatch.refusals` and `cloudwatch_queries` for the metric side, and never "
            "let one branch's silence stand in for the other's result. "
            "METRIC EVIDENCE (`kind: cloudwatch_metric`) carries CATEGORIES only — direction, "
            "variability, threshold_relation, points_seen. The datapoints are computed on and "
            "discarded, so there is no average/min/max/latest to quote and asking for one will not "
            "produce it. Report these honestly: `points_seen: 0` means no datapoint in EXACTLY "
            "that window, NOT that the service was healthy — many metrics are only published while "
            "traffic flows; and a direction is a shape, never a root cause. The alarm name is "
            "never guessed: pass `alarm_name` when the user gives you one. "
            "Pass the alert text verbatim. "
            "TIME WINDOW — this is a HARD STOP, not a caveat: the log tool backtracks from a full "
            "`alert_time`, so a runnable plan needs BOTH a date+time AND a timezone. If "
            "`plan.refusals` contains a BLOCKING window refusal, the plan was not runnable and ZERO "
            "log queries were made — nothing was read, so do not describe the logs at all. Read "
            "WHICH HALF it says is missing and ask only for that: "
            "a missing DATE ('the alert says 03:15 — which day?') or a missing TIMEZONE ('this "
            "03:15 — HKT or UTC?'). Then call again passing `alert_time` as "
            "'YYYY-MM-DD HH:MM:SS' and/or `timezone` as an IANA zone. "
            "Pass ONLY what the user actually told you — never compute 'yesterday' or fill in "
            "today's date yourself; guessing the day fails exactly like guessing the zone. "
            "The reason is worth telling them: CloudWatch writes UTC, "
            "LogDream defaults to Asia/Hong_Kong and the servers are GMT, so the same clock time can "
            "be three moments 8 hours apart; searching the wrong one returns nothing, and nothing "
            "reads as 'no problem'. "
            "WHERE TO LOOK — pass `repos` with repo ids you already know are involved (from "
            "`incident_impact`, from the user, from the conversation). Targets are otherwise taken "
            "ONLY from repo ids appearing in the alert text, and the largest alert family here "
            "names no repo at all. Ids are validated against the repo universe: an unknown id is "
            "refused, never queried, so pass the exact id and never invent one. Caller-supplied "
            "targets are marked in `plan.targets[].source` — report that, since a nil result on a "
            "repo YOU named is weaker than one on a repo the alert identified. "
            "RE-CALLABLE WITHIN THE SAME TURN, and one sweep is a starting point, not a verdict. "
            "Go again, changing something, when: `queries_executed` is shorter than "
            "`queries_attempted`; a target's `app_resolved` is empty; `evidence` is empty though "
            "the plan WAS runnable (other `keywords`, the other `sources`, a wider `max_queries`); "
            "or the evidence names a downstream service you have not searched (add it to `repos`). "
            "Stop when the evidence answers the question, or the next call would repeat the last. "
            "Re-calling is pointless for exactly one refusal: a BLOCKING window refusal means "
            "nothing was read and only the user can fix it — ask, do not retry. "
            "DRILL-DOWN — for a follow-up like '再宽一点时间窗', '换 ConnectException 再查', "
            "'只看 hkp3', '多查几个关键词': call this again with `keywords` (REPLACES the "
            "graph-derived list — spend the budget on what they asked for), `sources` (subset of "
            "the configured LogDream sources, e.g. hkl/hkp3 — read `plan.sources` for the live "
            "list rather than typing one; they are ALL production with DIFFERENT content, so say "
            "which you searched), "
            "and/or `max_queries` (raise it when they want a wider sweep and are willing to wait — "
            "each read can take ~3-10s). Report that the keywords were user-supplied rather than "
            "derived, because a nil result on a derived keyword list means much more than a nil "
            "result on one term someone guessed. "
            "It runs in an isolated investigator that holds the raw logs in memory and returns only "
            "a REDACTED, aggregated evidence packet: counts, exception classes, and at most 5 "
            "short redacted lines per finding. "
            "IMPORTANT — report these honestly: (1) read `plan` and `queries_executed` and say which "
            "app, LOG FILE, keywords and window were actually read; a nil result only means nil FOR "
            "THOSE, and the keywords come from our code graph, not from the log. `queries_attempted` "
            "minus `queries_executed` is work that never reached the server — never describe it as "
            "searched. A `not_investigated` entry saying 'The query SUCCEEDED' is OUR parser failing "
            "on their response shape, NOT an empty log: report it as a wiring problem to fix in "
            "config/mcp_tools.json, never as 'no errors found'; (2) `not_investigated` is not the same as "
            "'nothing found' — a server that did not respond, an app name that could not be "
            "verified, or a missing timezone all land there, and each means 'we did not look', "
            "which you must say out loud instead of implying the logs were clean; (3) every item "
            "carries `environment` — logs are PRODUCTION, the use-case route snapshot is dev/SCT, "
            "so never assert production behaviour from the snapshot; (4) excerpts are redacted "
            "(`<phone:ab12>` style); read `storage_rule` — when raw retention is ON for the UAT "
            "test, each evidence item has a `raw_ref` and the USER can click through to the "
            "original, but YOU still cannot read it, so offer the click-through and never imply you "
            "verified the original yourself; when it is off, the raw text is gone and no code path "
            "returns it; (5) exception classes and counts are evidence, but a log line is not a root "
            "cause on its own — say what it shows, not what you infer.",
            {"alert_text": {"type": "string"}, "timezone": {"type": "string"},
             "alert_time": {"type": "string"}, "alarm_name": {"type": "string"},
             "keywords": {"type": "array", "items": {"type": "string"}},
             "sources": {"type": "array", "items": {"type": "string"}},
             "repos": {"type": "array", "items": {"type": "string"}},
             "tracking_id": {"type": "string"},
             "portal_channel": {"type": "string", "enum": ["sms", "email", "auto"]},
             "max_queries": {"type": "integer"}},
            ["alert_text"]),
    _schema("db_query",
            "LIVE READ-ONLY query against the UAT database, run through the intranet's own skill. "
            "You cannot write SQL and there is no way to ask for one: pick a `query` NAME the "
            "intranet has declared and pass its `params`. Call with NO arguments first to see the "
            "catalog — which names exist, which are actually wired, and what each one needs; add "
            "`check=true` to also test the connection. "
            "THREE things about the answer that you must carry into yours: (1) it is UAT, not "
            "production — the data may be copied, masked or synthetic, so never state a UAT row as "
            "a production fact; (2) `ok:false` means the query DID NOT RUN — 'not wired', 'not "
            "ready' and 'refused' are never 'there is no such record', and a packet without a "
            "`rows` key is not an empty result; (3) this supplements the code graph, the snapshots "
            "and the MCP evidence — it does not replace them, so say which of your statements came "
            "from a live UAT query and which came from a snapshot or from code.",
            {"query": {"type": "string"}, "params": {"type": "object"},
             "check": {"type": "boolean"}}),
]

# Tools whose result is a sub-agent evidence packet, so it is charged to the `subagent` context lane
# instead of the shared `tools` lane. A log packet arriving mid-turn must not eat the room the other
# tools in that same turn still need.
SUBAGENT_TOOLS = frozenset({"incident_investigate"})


def dispatch_events(name, a, owner=""):
    """Streaming dispatch for sub-agent tools: yield progress events, then a terminal `result`.

    `owner` is the opaque per-browser id retained raw logs are scoped to. Empty (the CLI/MCP path)
    means anything retained is readable by nobody, which is the right default: there is no browser to
    click through from.

    Exists so the agent loop can relay a sub-agent's steps to the browser without threads or
    queues. A 30-second log sweep behind an opaque spinner is what makes people distrust an agent
    that is working correctly, so the same events that prove it is working are the ones shown.
    Anything not in SUBAGENT_TOOLS yields exactly one `result`, so callers need no special case.
    """
    a = a or {}
    if name == "incident_investigate":
        text = (a.get("alert_text") or "").strip()
        if not text:
            yield {"type": "result",
                   "packet": {"ok": False,
                              "error": "alert_text is required (paste the raw alert verbatim)"}}
            return
        # Imported here, not at module import time: this is the only tool that can reach production,
        # and keeping the import local means the retrieval-only tool surface does not depend on it.
        from . import incident_investigator
        for event in incident_investigator.investigate_events(
                text,
                timezone=(a.get("timezone") or "").strip() or None,
                alert_time=(a.get("alert_time") or "").strip() or None,
                alarm_name=(a.get("alarm_name") or "").strip() or None,
                keywords=a.get("keywords") or None,
                sources=a.get("sources") or None,
                # WHERE to look, when the caller knows it and the alert text does not say.
                target_repos=a.get("repos") or None,
                # The Portal delivery-record branch. Entered through this ONE isolated tool, never
                # as a free-standing MCP tool, so it shares the same exit gate and the same
                # production-query master switch as the log and metric branches.
                tracking_id=(a.get("tracking_id") or "").strip() or None,
                portal_channel=(a.get("portal_channel") or "auto").strip().lower(),
                max_queries=a.get("max_queries") or None,
                owner=owner):
            yield event
        return
    yield {"type": "result", "packet": dispatch(name, a)}


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
    if name == "incident_impact":
        text = (a.get("alert_text") or "").strip()
        if not text:
            return {"ok": False, "error": "alert_text is required (paste the raw alert verbatim)"}
        return incident.incident_impact(text, max_use_cases=int(a.get("max_use_cases") or 40))
    if name == "critical_repos":
        return criticality.rank(top=int(a.get("top") or 10))
    if name == "incident_investigate":
        result = {}
        for event in dispatch_events(name, a):
            if event.get("type") == "result":
                result = event["packet"]
        return result
    if name == "show_coverage":
        return _coverage_view(a.get("kind"), a.get("value"))
    if name == "db_query":
        # Imported here, not at module import time: the retrieval-only tool surface must not depend
        # on the database path existing, exactly as with incident_investigator.
        from . import db_readonly
        query = (a.get("query") or "").strip()
        if not query:
            return db_readonly.catalog(probe=bool(a.get("check")))
        # `caller="product"` is the model asking. It is the stricter setting, and the default in
        # db_registry is 'internal', so wiring a query's SQL does not by itself hand it to the chat.
        return db_readonly.run(query, a.get("params") or {}, caller="product")
    return {"error": f"unknown tool: {name}"}
