"""Resolve a channel/vendor to the arch-map nodes on its affected chain, for the inline diagram.

Mirrors the highlight logic in ``static/arch.html`` (computeHighlight) so the assistant and the page
agree on which nodes light up. Used by the ``show_arch`` tool: the Q&A embeds the architecture
diagram in its answer with exactly this chain highlighted, so a non-technical user never has to open
a page or click a node themselves.
"""
import json

from . import config

ROUTERS = ("decision-topics", "decision-job")


def _load_nodes():
    try:
        with open(config.ARCH_NODES_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return []
    nodes = data.get("nodes") if isinstance(data, dict) else data
    return [n for n in (nodes or []) if isinstance(n, dict) and n.get("id")]


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


def focus(kind, value):
    """Return a directive for the inline arch view, or ``ok: False`` with the valid options."""
    kind = (kind or "").strip().lower()
    value = (value or "").strip()
    nodes = _load_nodes()
    if kind not in ("channel", "vendor"):
        return {"ok": False, "error": "kind must be 'channel' or 'vendor'",
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
