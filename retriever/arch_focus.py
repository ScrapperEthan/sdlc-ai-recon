"""Resolve a channel/vendor to the arch-map nodes on its affected chain, for the inline diagram.

Mirrors the highlight logic in ``static/arch.html`` (computeHighlight) so the assistant and the page
agree on which nodes light up. Used by the ``show_arch`` tool: the Q&A embeds the architecture
diagram in its answer with exactly this chain highlighted, so a non-technical user never has to open
a page or click a node themselves.
"""
import json

from . import config, usecase_master

ROUTERS = ("decision-topics", "decision-job")
# The business-source gutter's downstream chain when no precise adapter edge is known (Tier 1
# will resolve the real source_system -> DSP/File/MQ/Kafka adapter split).
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


def _focus_business_source(kind, value):
    """kind='source-system' focuses that system directly; kind='use-case' resolves the id's
    declared source_system via usecase_master.master_for first. Honesty note: this lights up the
    use case's DECLARED upstream system (cited to the master row), not a discovered code edge —
    the precise source_system -> ingress adapter split is Tier 1."""
    resolved, note = value, None
    if kind == "use-case":
        master = usecase_master.master_for(value)
        source_system = (master or {}).get("source_system") or ""
        if not source_system:
            return {"ok": False, "error": f"no declared source_system for use-case:{value}"}
        resolved = source_system
        note = f"resolved from use-case:{value} (master snapshot)"

    match = _business_source_for(resolved)
    if not match:
        options = sorted({s.get("source_system") for s in _business_sources() if s.get("source_system")})
        return {"ok": False, "error": f"unknown source-system: {resolved}", "source_systems": options}

    hit = {match["id"], *EARLY_SPINE}
    if match.get("edge_to"):
        hit.add(match["edge_to"])
    highlight = f"source-system:{resolved.lower()}"
    summary = f"已在架构图左侧高亮业务上游「{resolved}」的声明入口（非发现的代码边，来自 Use Case 主数据）。"
    result = {
        "ok": True,
        "view": "arch",
        "highlight": highlight,
        "url": f"/arch.html?embed=1&highlight={highlight}",
        "kind": "source-system",
        "value": resolved.lower(),
        "affected_node_ids": sorted(hit),
        "affected_node_count": len(hit),
        "summary": summary,
    }
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
