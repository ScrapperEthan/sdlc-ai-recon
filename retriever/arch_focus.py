"""Resolve a channel/vendor to the arch-map nodes on its affected chain, for the inline diagram.

Mirrors the highlight logic in ``static/arch.html`` (computeHighlight) so the assistant and the page
agree on which nodes light up. Used by the ``show_arch`` tool: the Q&A embeds the architecture
diagram in its answer with exactly this chain highlighted, so a non-technical user never has to open
a page or click a node themselves.
"""
import json
import urllib.parse

from . import config, delivery_chain, usecase_master

ROUTERS = ("decision-topics", "decision-job")
# The business-source gutter's downstream chain when no precise adapter edge is known (Tier 1
# will resolve the real source_system -> DSP/File/MQ/Kafka adapter split).
#
# EARLY_SPINE is the INGRESS half only. Focusing a use case used to stop here — the diagram lit up
# columns 0-2 and went dark exactly where the reader's question usually starts ("so what actually
# goes out, through whom?"). `_exit_spine` below carries the highlight through topics, delivery
# jobs, outbound APIs and on to the carrier terminal; see retriever/delivery_chain.py for what is
# fact (the stages) and what is an upper bound (which carrier this particular use case uses).
EARLY_SPINE = ("ingress-api",) + ROUTERS


def _load_catalog():
    try:
        with open(config.ARCH_NODES_JSON, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _load_nodes():
    data = _load_catalog()
    nodes = data.get("nodes") if isinstance(data, dict) else data
    return [n for n in (nodes or []) if isinstance(n, dict) and n.get("id")]


def _business_sources():
    data = _load_catalog()
    items = data.get("business_sources") if isinstance(data, dict) else None
    return [b for b in (items or []) if isinstance(b, dict) and b.get("id")]


def _channels(nodes):
    return sorted({(n.get("channel") or "").lower() for n in nodes if n.get("channel")})


def _vendors(nodes):
    return sorted({(n.get("vendor") or "").lower() for n in nodes if n.get("vendor")})


def affected_nodes(nodes, kind, value):
    """Node-id set on the affected chain — the same rule the page uses.

    channel:X → every node of channel X + the decision routers.
    vendor:Y  → Y's own nodes + the shared topics/delivery-job of Y's channel (NOT the other
                vendors' outbound/terminal nodes) + the decision routers.
    """
    value = (value or "").lower()
    hit = set()
    if kind == "channel":
        for node in nodes:
            if (node.get("channel") or "").lower() == value:
                hit.add(node["id"])
        hit.update(ROUTERS)
    elif kind == "vendor":
        channels = set()
        for node in nodes:
            if (node.get("vendor") or "").lower() == value:
                hit.add(node["id"])
                if node.get("channel"):
                    channels.add(node["channel"].lower())
        for node in nodes:
            if (node.get("channel") or "").lower() in channels and node.get("role") in ("topic", "delivery-job"):
                hit.add(node["id"])
        hit.update(ROUTERS)
    return hit


def _business_source_for(value):
    needle = (value or "").strip().lower()
    for source in _business_sources():
        if (source.get("source_system") or "").strip().lower() == needle:
            return source
        if (source.get("label") or "").strip().lower() == needle:
            return source
    return None


# Round B6: edge-confidence tiers (spec §9.3) for the business-source -> ingress edge.
_CONFIDENCE_DECLARED = "declared-db"          # Ext.endpoint resolved to a known repo
_CONFIDENCE_UNVERIFIED_RAW = "inferred-normalization"  # endpoint present but unresolved
_CONFIDENCE_UNVERIFIED_NONE = "generic-unverified"     # no endpoint declared at all


def _endpoint_evidence_for_use_case(use_case_id):
    """{"endpoint_repos": [...], "edge_confidence": tier} for one use case's declared entrypoint."""
    ext = usecase_master.ext_by_use_case_id().get((use_case_id or "").strip().lower())
    endpoint_repos = usecase_master.resolve_endpoint(ext.get("endpoint") or "") if ext else []
    resolved = [seg for seg in endpoint_repos if seg.get("repo")]
    if resolved:
        confidence = _CONFIDENCE_DECLARED
    elif endpoint_repos:
        confidence = _CONFIDENCE_UNVERIFIED_RAW
    else:
        confidence = _CONFIDENCE_UNVERIFIED_NONE
    return {"endpoint_repos": endpoint_repos, "edge_confidence": confidence}


def _endpoint_evidence_for_source_system(source_system):
    """Aggregate endpoint-repo evidence across a source_system's members: {top_repos: [(repo,
    count), ...], edge_confidence: tier, sample_size}. Round A/B4 already resolve per-item
    endpoint_repos in use_cases_for_source_system; this just tallies them, capped to a reasonable
    sample so a ~880-member source_system doesn't do unbounded work here."""
    members = usecase_master.use_cases_for_source_system(source_system, limit=200)
    items = members.get("items") or []
    counts = {}
    any_declared = False
    for item in items:
        for seg in item.get("endpoint_repos") or []:
            if seg.get("repo"):
                any_declared = True
                counts[seg["repo"]] = counts.get(seg["repo"], 0) + 1
    top = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    if top:
        confidence = _CONFIDENCE_DECLARED
    elif any_declared:
        confidence = _CONFIDENCE_UNVERIFIED_RAW
    else:
        confidence = _CONFIDENCE_UNVERIFIED_NONE
    return {"top_repos": top, "edge_confidence": confidence, "sample_size": len(items)}


def _exit_spine(use_case_id):
    """Node ids for a use case's last mile, plus a one-line summary of where it exits.

    Channels come from the use case's channel rules when it has them, and are otherwise inferred
    from nothing at all — no rules means no basis for lighting a specific channel, and lighting
    every channel would tell the reader that this use case sends on all of them. Returns an empty
    spine in that case, and the caller keeps the ingress-only highlight it had before.
    """
    rules = usecase_master.rules_by_use_case_id().get((use_case_id or "").strip().lower()) or []
    channels = sorted({rule["channel"] for rule in rules if rule.get("channel")})
    if not channels:
        return set(), None
    path = delivery_chain.exit_path(channels, rules=rules)
    if not path.get("available"):
        return set(), None
    node_ids = {stage["node_id"] for item in path["by_channel"] for stage in item["stages"]
                if stage.get("node_id")}
    exits = "、".join(path["terminals"]) or "未在架构图上绘出"
    carriers = "、".join(path["vendors"]) or "未识别"
    bound = "（按渠道取上界，非该用例实际厂商）" if any(
        item["vendor_selection"]["method"] == "channel_upper_bound" for item in path["by_channel"]
    ) else "（由 route/router 收窄，属线索）"
    summary = f"出口：{exits}；厂商：{carriers}{bound}。"
    return node_ids, summary


def _focus_business_source(kind, value):
    """kind='source-system' focuses that system directly; kind='use-case' resolves the id's
    declared source_system via usecase_master.master_for first. Honesty note: this lights up the
    use case's DECLARED upstream system (cited to the master row), not a discovered code edge.

    Round B6: no longer limited to the 5 statically pre-declared nodes in arch_nodes.json — any
    canonicalized source_system with at least one known use case resolves (synthesizing a gutter
    node on the fly), matching the real ~150-value UAT universe. When endpoint-repo evidence is
    available (Ext.endpoint resolved to a known repo), it's surfaced with a confidence tag instead
    of the generic "declared entrypoint" — see _endpoint_evidence_for_*."""
    resolved, note = value, None
    endpoint_repos, edge_confidence, extra_note = [], _CONFIDENCE_UNVERIFIED_NONE, None
    if kind == "use-case":
        master = usecase_master.master_for(value)
        source_system = (master or {}).get("source_system") or ""
        if not source_system:
            return {"ok": False, "error": f"no declared source_system for use-case:{value}"}
        resolved = source_system
        note = f"resolved from use-case:{value} (master snapshot)"
        evidence = _endpoint_evidence_for_use_case(value)
        endpoint_repos = evidence["endpoint_repos"]
        edge_confidence = evidence["edge_confidence"]
        resolved_names = [seg["repo"] for seg in endpoint_repos if seg.get("repo")]
        if resolved_names:
            extra_note = f"入口 repo: {', '.join(sorted(set(resolved_names)))}（{edge_confidence}）"

    match = _business_source_for(resolved)
    if not match:
        canon = usecase_master.canonicalize_source_system(resolved)
        known_canonicals = {s["canonical"] for s in usecase_master.source_systems()}
        if canon["canonical"] not in known_canonicals:
            options = sorted({s.get("source_system") for s in _business_sources() if s.get("source_system")}
                              | {s["display_name"] for s in usecase_master.source_systems()})
            return {"ok": False, "error": f"unknown source-system: {resolved}", "source_systems": options}
        # Dynamically synthesize a gutter node for a real (but not statically pre-declared)
        # source_system, so focus works for the full ~150-value universe, not just the 5 in the
        # static overview list.
        match = {"id": "biz-dyn-" + canon["canonical"], "label": canon["display_name"],
                  "source_system": canon["display_name"], "edge_to": "ingress-api", "dynamic": True}
        if kind != "use-case":  # use-case path already computed evidence for its ONE use case
            agg = _endpoint_evidence_for_source_system(resolved)
            edge_confidence = agg["edge_confidence"]
            if agg["top_repos"]:
                names = ", ".join(f"{repo}×{count}" for repo, count in agg["top_repos"])
                extra_note = f"高频入口 repo（样本 {agg['sample_size']}）: {names}（{edge_confidence}）"

    hit = {match["id"], *EARLY_SPINE}
    if match.get("edge_to"):
        hit.add(match["edge_to"])
    exit_nodes, exit_summary = _exit_spine(value) if kind == "use-case" else (set(), None)
    hit |= exit_nodes
    highlight = f"source-system:{resolved.lower()}"
    summary = f"已在架构图左侧高亮业务上游「{resolved}」的声明入口（非发现的代码边，来自 Use Case 主数据）。"
    if extra_note:
        summary += extra_note
    if exit_summary:
        summary += "已一路高亮到最终出口。" + exit_summary
    elif kind == "use-case":
        summary += "该用例没有渠道规则行，无法确定它走哪条渠道，因此只高亮到 Decision，未点亮任何出口。"
    # Carry the properly-cased display label through the URL — arch.html reconstructs the gutter
    # node client-side purely from the URL (no callback to this function), so without this a
    # dynamically-synthesized node (match["label"], set above) would fall back to the raw
    # lower-cased `value` and show e.g. "powercard" instead of "PowerCard".
    label_param = urllib.parse.quote(match.get("label") or resolved, safe="")
    # The page recomputes a source-system highlight from the URL alone and only knows the ingress
    # half; the exit half depends on this use case's channel rules, which the page cannot see. Pass
    # the resolved ids explicitly so the diagram and the answer text agree (arch.html applyDeepLink).
    nodes_param = ("&nodes=" + urllib.parse.quote(",".join(sorted(exit_nodes)), safe=",")
                   if exit_nodes else "")
    result = {
        "ok": True,
        "view": "arch",
        "highlight": highlight,
        "url": f"/arch.html?embed=1&highlight={highlight}&label={label_param}{nodes_param}",
        "kind": "source-system",
        "value": resolved.lower(),
        "affected_node_ids": sorted(hit),
        "affected_node_count": len(hit),
        "edge_confidence": edge_confidence,
        "summary": summary,
    }
    if endpoint_repos:
        result["endpoint_repos"] = endpoint_repos
    if note:
        result["note"] = note
    return result


def focus(kind, value):
    """Return a directive for the inline arch view, or ``ok: False`` with the valid options."""
    kind = (kind or "").strip().lower()
    value = (value or "").strip()

    if kind in ("source-system", "use-case"):
        return _focus_business_source(kind, value)

    nodes = _load_nodes()
    if kind not in ("channel", "vendor"):
        return {"ok": False, "error": "kind must be 'channel', 'vendor', 'source-system' or 'use-case'",
                "channels": _channels(nodes), "vendors": _vendors(nodes)}
    valid = _channels(nodes) if kind == "channel" else _vendors(nodes)
    if value.lower() not in valid:
        return {"ok": False, "error": f"unknown {kind}: {value}", f"{kind}s": valid}
    hit = sorted(affected_nodes(nodes, kind, value))
    if not hit:
        return {"ok": False, "error": f"no arch nodes for {kind}:{value}"}
    highlight = f"{kind}:{value.lower()}"
    return {
        "ok": True,
        "view": "arch",
        "highlight": highlight,
        "url": f"/arch.html?embed=1&highlight={highlight}",
        "kind": kind,
        "value": value.lower(),
        "affected_node_ids": hit,
        "affected_node_count": len(hit),
        "summary": f"已在架构图上高亮 {highlight} 的受影响链路，共 {len(hit)} 个节点。",
    }
