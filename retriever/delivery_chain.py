"""The LAST MILE: a use case's declared channels -> topics -> delivery jobs -> outbound APIs ->
carrier -> the thing the customer actually receives.

Why this module exists. Everything the assistant could previously say about a use case stopped at
the *ingress* half of the pipeline: the declared upstream source_system, the ingress API, the
decision job, and whichever repos happened to sit on the routing snapshot's topics. Asked for the
"full chain", it answered with the first three of seven columns and never named a carrier or an
exit. `retriever/arch_focus.py` had the same ceiling — focusing a use case highlighted
``EARLY_SPINE`` (ingress + decision) and nothing downstream of it.

The pipeline's shape is not a guess: ``static/arch_nodes.json`` is a committed catalog of all 7
columns with explicit edges, ending in the real terminals (CSL SMSC, 3HK SMSC, APNs/FCM,
ProofPoint, print/mail, …). This module walks that graph FORWARD from each declared channel's topic
node and binds every stage it passes to real repos from ``index/delivery_topology.json``.

What is fact and what is not — this distinction is the whole point, so it is carried in the payload
rather than left to the caller's prose:

* **Structure** (which stages exist, in what order, which carrier serves which channel) is fact:
  a committed catalog plus a repo-name-derived topology whose parser has been box-verified through
  RUNBOOK-49/50/51/52.
* **Which carrier THIS use case uses** is generally NOT known. So the default answer is every
  carrier on the channel, labelled ``channel_upper_bound``: "at most these", never "these". The
  authoritative vendor column lives in `tbl_use_case_router`, which the intranet HAS now ingested
  (RUNBOOK-54, 247 rows) but which nothing joins yet — the reason travels with the caveat at
  runtime via ``usecase_catalog.router_table_status()`` instead of being frozen into a string here,
  because the previous frozen version went on saying "not ingested" after it was.
* When ``tbl_use_case_channel_rule.route``/``.router``/``.sender`` names a known carrier (values
  look like ``CSL_SVC_RT_SMS``), the set is narrowed and labelled ``route_hint`` — a HINT, because
  that those columns carry the carrier is still unconfirmed (RUNBOOK-54 question 1).

An honest wide answer beats a confident narrow one: over-listing carriers costs the reader a
sentence, while inventing the wrong one gets the wrong vendor called at 3am.

The static catalog is a hand-drawn diagram and the topology is derived from real repo names, so the
two can disagree. Where the topology knows a carrier the diagram never drew, it is still reported
(``off_diagram``) — under-reporting the exit is the one failure this module exists to prevent.
"""
import json
import os

from . import config, usecase_catalog
from .vendors import KNOWN_VENDORS, UNKNOWN_VENDOR, canon_vendor, vendors_in

# Raw DB channel value -> the delivery channel the architecture actually has an exit for.
# TWOWAYSMS rides the SMS last mile (owner-confirmed 3HK flow, RUNBOOK-50); PUSH+INBOX / PUSH_INBOX
# is the same naming drift the rule_text parser already tolerates (DB value vs Java enum).
# A value that is NOT here is reported in `unmapped_channels` rather than dropped — a new channel
# in the DB must show up as a visible gap, not as a silently shorter chain.
_CHANNEL_ALIASES = {
    "SMS": "sms", "TWOWAYSMS": "sms", "2WAYSMS": "sms", "TWO_WAY_SMS": "sms",
    "MMS": "mms",
    "EMAIL": "email", "MAIL": "email",
    "LETTER": "letter",
    "WHATSAPP": "whatsapp",
    "WECHAT": "wechat",
    "PUSH": "push", "PUSH_INBOX": "push", "PUSH+INBOX": "push", "PUSHINBOX": "push",
}

# Pipeline order. The catalog calls the terminal column `external`; renamed to `vendor-terminal`
# on the way out because "external" is also its word for datastores and upstream sources, and the
# reader of an exit path should not have to disambiguate.
_STAGE_RANK = {"topic": 0, "delivery-job": 1, "outbound-api": 2, "vendor-terminal": 3}

_UPPER_BOUND_CAVEAT = (
    "carrier set is an UPPER BOUND: these are every carrier serving this channel, not the one this "
    "use case actually routes to. Say 'at most these carriers', never 'it sends via X'."
)


def _upper_bound_caveat():
    """The upper-bound caveat plus the CURRENT reason it is still an upper bound.

    Hardcoding "tbl_use_case_router is not ingested" made this text go stale the moment the
    intranet ingested it (RUNBOOK-54, 247 rows): the answer then told the reader to go get
    something they already had. The reason is read from the dataset instead."""
    return _UPPER_BOUND_CAVEAT + " " + usecase_catalog.router_table_status()["note"]


_ROUTE_HINT_CAVEAT = (
    "carrier narrowed from tbl_use_case_channel_rule.route/router/sender, which is a HINT, not a "
    "confirmed mapping: that those columns carry the carrier is still unverified (RUNBOOK-54 "
    "question 1). Cite the rule row and say 'the route value names X'."
)
_OFF_DIAGRAM_NOTE = (
    "carrier is present in the repo topology but not drawn on the static architecture diagram, so "
    "its outbound/terminal nodes are named from repos only"
)


def _display_path(path):
    try:
        rel = os.path.relpath(path, config.ROOT)
    except ValueError:
        rel = path
    return rel.replace(os.sep, "/")


def _load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _catalog():
    """{nodes: {id: node}, forward: {id: [id, …]}, lines: [...]} from the committed node catalog.

    ``uses`` edges (decision job -> Redis/Postgres/OpenSearch) are dependencies, not delivery hops;
    walking them would drag datastores into the send path.
    """
    data = _load_json(config.ARCH_NODES_JSON)
    nodes = {}
    for node in data.get("nodes") or []:
        if isinstance(node, dict) and node.get("id"):
            nodes[node["id"]] = node
    forward = {}
    for edge in data.get("edges") or []:
        if not isinstance(edge, list) or len(edge) < 2:
            continue
        if len(edge) > 2 and edge[2] == "uses":
            continue
        forward.setdefault(edge[0], []).append(edge[1])
    try:
        with open(config.ARCH_NODES_JSON, encoding="utf-8-sig") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []
    return {"nodes": nodes, "forward": forward, "lines": lines}


def canonical_channel(raw):
    """A raw DB channel value -> the canonical delivery channel, or "" when there is no mapping."""
    key = str(raw or "").strip().upper().replace("-", "_").replace(" ", "")
    if key in _CHANNEL_ALIASES:
        return _CHANNEL_ALIASES[key]
    # Tolerate compound/decorated values (`SMS(HK)`, `EMAIL_RETAIL`) without inventing a channel
    # for a token that merely contains one — longest alias first so PUSH_INBOX beats PUSH.
    for alias in sorted(_CHANNEL_ALIASES, key=len, reverse=True):
        if key.startswith(alias):
            return _CHANNEL_ALIASES[alias]
    return ""


def vendor_hints(rules):
    """Carrier hints mined from a use case's channel rules: {channel: {vendor: [citation, …]}}.

    Reads `route`, `router` and `sender` — whichever names a known carrier. Rules whose channel
    doesn't map are skipped here; the caller reports them via `unmapped_channels`.
    """
    hints = {}
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        channel = canonical_channel(rule.get("channel"))
        if not channel:
            continue
        for field in ("route", "router", "sender"):
            for vendor in vendors_in(rule.get(field)):
                bucket = hints.setdefault(channel, {}).setdefault(vendor, [])
                citation = rule.get("citation")
                if citation and citation not in bucket:
                    bucket.append(citation)
    return hints


def _topology_repos(topology, channel, vendor, key):
    group = ((topology.get(channel) or {}).get(vendor) or {})
    if not isinstance(group, dict):
        return []
    return sorted({
        entry["repo"] for entry in group.get(key) or []
        if isinstance(entry, dict) and entry.get("repo")
    })


def _channel_vendors(topology, channel):
    vendors = topology.get(channel)
    if not isinstance(vendors, dict):
        return set()
    return {name for name in vendors if isinstance(name, str) and name != UNKNOWN_VENDOR}


def _walk(catalog, start_id, allowed_vendors):
    """Forward-reachable nodes from `start_id`, pruned at any node whose vendor is excluded.

    Pruning DURING the walk (rather than filtering the result) matters: a terminal that declares no
    vendor of its own — ext-3hk-mmsc, ext-iccm-otx — must still disappear when the outbound node
    that leads to it was excluded, or a narrowed answer would sprout a carrier's terminal with the
    carrier's own API filtered out.
    """
    nodes, forward = catalog["nodes"], catalog["forward"]
    if start_id not in nodes:
        return []
    seen, order, queue = {start_id}, [start_id], [start_id]
    while queue:
        current = queue.pop(0)
        for nxt in forward.get(current) or []:
            if nxt in seen or nxt not in nodes:
                continue
            vendor = canon_vendor(nodes[nxt].get("vendor"))
            if vendor and allowed_vendors is not None and vendor not in allowed_vendors:
                continue
            seen.add(nxt)
            order.append(nxt)
            queue.append(nxt)
    return [nodes[node_id] for node_id in order]


def _node_citation(catalog, node_id):
    for line_no, line in enumerate(catalog["lines"], 1):
        if f'"{node_id}"' in line:
            return f"{_display_path(config.ARCH_NODES_JSON)}:{line_no}"
    return ""


def _stage_entry(catalog, node, channel, topology, topics, allowed):
    """One pipeline stage, bound to the repos that actually implement it.

    Vendor-less stages (the shared `sms-deli` job node, say) bind every ALLOWED carrier's repos —
    passing `allowed` through is what keeps a narrowed answer narrow at the repo level too, instead
    of naming one carrier in the heading and listing all of them underneath.
    """
    role = (node.get("role") or "").strip().lower()
    vendor = canon_vendor(node.get("vendor"))
    entry = {
        "stage": "vendor-terminal" if role == "external" else role,
        "node_id": node["id"],
        "label": node.get("label") or node["id"],
        "detail": node.get("sub") or "",
        "vendor": vendor or None,
        "repos": [],
        "topics": [],
        "citations": [c for c in [_node_citation(catalog, node["id"])] if c],
    }
    if role == "external":
        entry["third_party"] = True
    key = {"delivery-job": "delivery_jobs", "outbound-api": "outbound_apis"}.get(role)
    if key:
        bind = [vendor] if vendor else sorted(
            _channel_vendors(topology, channel) & allowed if allowed is not None
            else _channel_vendors(topology, channel))
        repos = set()
        for name in bind:
            repos.update(_topology_repos(topology, channel, name, key))
        entry["repos"] = sorted(repos)
    elif role == "topic":
        # Kafka topics are not repos. Bind the use case's own routed topics when the topic name
        # carries the channel; an empty list here means "this snapshot doesn't show the topic",
        # never "there is no topic".
        entry["topics"] = sorted({t for t in topics or [] if channel in (t or "").lower()})
    if entry["repos"]:
        entry["citations"].append(_display_path(config.DELIVERY_TOPOLOGY_JSON))
    return entry


def _off_diagram_stages(channel, topology, vendors):
    """Outbound stages for carriers the topology knows but the static diagram never drew."""
    stages = []
    for vendor in sorted(vendors):
        repos = _topology_repos(topology, channel, vendor, "outbound_apis")
        if not repos:
            continue
        stages.append({
            "stage": "outbound-api",
            "node_id": None,
            "label": f"{vendor.upper()} 出站 API",
            "detail": _OFF_DIAGRAM_NOTE,
            "vendor": vendor,
            "off_diagram": True,
            "repos": repos,
            "topics": [],
            "citations": [_display_path(config.DELIVERY_TOPOLOGY_JSON)],
        })
    return stages


def _summarize(channel, stages):
    """One readable line per channel — the thing a non-technical reader actually wants."""
    parts = []
    for stage in stages:
        label = stage["label"]
        if stage["repos"]:
            shown = ", ".join(stage["repos"][:3])
            if len(stage["repos"]) > 3:
                shown += f", +{len(stage['repos']) - 3}"
            label = f"{label}({shown})"
        elif stage["topics"]:
            label = f"{label}({', '.join(stage['topics'][:2])})"
        parts.append(label)
    if not parts:
        return f"{channel.upper()}: (no path)"
    return f"{channel.upper()}: " + " → ".join(parts)


def _channel_path(catalog, channel, declared_as, topology, topics, hints):
    channel_hints = hints.get(channel) or {}
    narrowed = sorted(v for v in channel_hints if v in KNOWN_VENDORS)
    allowed = set(narrowed) if narrowed else None

    walked = _walk(catalog, _topic_node_id(catalog, channel), allowed)
    stages = [_stage_entry(catalog, node, channel, topology, topics, allowed) for node in walked]

    drawn = {stage["vendor"] for stage in stages if stage.get("vendor")}
    known = _channel_vendors(topology, channel)
    off_diagram = (known & allowed if allowed is not None else known) - drawn
    stages.extend(_off_diagram_stages(channel, topology, off_diagram))
    stages.sort(key=lambda s: (_STAGE_RANK.get(s["stage"], len(_STAGE_RANK)), s["label"]))

    vendors = sorted(drawn | off_diagram)
    return {
        "channel": channel,
        "declared_as": sorted(set(declared_as)),
        "stages": stages,
        "vendors": vendors,
        "vendors_off_diagram": sorted(off_diagram),
        "terminals": [s["label"] for s in stages if s["stage"] == "vendor-terminal"],
        "vendor_selection": {
            "method": "route_hint" if narrowed else "channel_upper_bound",
            "caveat": _ROUTE_HINT_CAVEAT if narrowed else _upper_bound_caveat(),
            "citations": sorted({c for v in narrowed for c in channel_hints.get(v) or []}),
        },
        "path_summary": _summarize(channel, stages),
    }


def _topic_node_id(catalog, channel):
    for node in catalog["nodes"].values():
        if (node.get("role") or "").lower() == "topic" and (node.get("channel") or "").lower() == channel:
            return node["id"]
    return ""


def exit_path(channels, rules=None, topics=None):
    """Declared channels -> the full exit path per channel, down to the carrier terminal.

    `channels` are raw DB values (`tbl_use_case_channel_rule.channel`). `rules` are that use case's
    channel-rule rows, mined for a carrier hint. `topics` are the routed topic names already known
    to the caller, used to fill the topic stage.

    Always returns a payload — a missing catalog or topology yields ``available: False`` with the
    reason, never an exception and never a silently short chain.
    """
    catalog = _catalog()
    topology = _load_json(config.DELIVERY_TOPOLOGY_JSON)
    result = {
        "available": False,
        "by_channel": [],
        "unmapped_channels": [],
        "vendors": [],
        "terminals": [],
        "caveats": [],
        "sources": [_display_path(config.ARCH_NODES_JSON),
                    _display_path(config.DELIVERY_TOPOLOGY_JSON)],
    }
    if not catalog["nodes"]:
        result["reason"] = (f"architecture node catalog not readable "
                            f"({_display_path(config.ARCH_NODES_JSON)}) — exit path unavailable")
        return result
    # `by_repo` is an index the builder appends, not a channel — a file containing only that is an
    # empty build, which must read as "no repos bound" rather than pass for a populated topology.
    if not any(key != "by_repo" and isinstance(value, dict) for key, value in topology.items()):
        result["note"] = (f"no delivery topology ({_display_path(config.DELIVERY_TOPOLOGY_JSON)} is "
                          "absent or empty); stages are structural only, with no repos bound — run "
                          "make_delivery_topology.py")

    hints = vendor_hints(rules)
    canonical = {}
    for raw in channels or []:
        channel = canonical_channel(raw)
        if channel and _topic_node_id(catalog, channel):
            canonical.setdefault(channel, []).append(raw)
        elif raw and raw not in result["unmapped_channels"]:
            result["unmapped_channels"].append(raw)

    for channel in sorted(canonical):
        result["by_channel"].append(
            _channel_path(catalog, channel, canonical[channel], topology, topics, hints))

    result["available"] = bool(result["by_channel"])
    result["vendors"] = sorted({v for item in result["by_channel"] for v in item["vendors"]})
    for item in result["by_channel"]:
        result["terminals"].extend(t for t in item["terminals"] if t not in result["terminals"])

    methods = {item["vendor_selection"]["method"] for item in result["by_channel"]}
    if "channel_upper_bound" in methods:
        result["caveats"].append(_upper_bound_caveat())
    if "route_hint" in methods:
        result["caveats"].append(_ROUTE_HINT_CAVEAT)
    if any(item["vendors_off_diagram"] for item in result["by_channel"]):
        result["caveats"].append(_OFF_DIAGRAM_NOTE + " — see `vendors_off_diagram`.")
    if result["unmapped_channels"]:
        result["caveats"].append(
            "declared channel(s) with no known exit path: "
            + ", ".join(result["unmapped_channels"])
            + " — report them as unmapped, do not fold them into another channel.")
    if not result["available"] and not result.get("reason"):
        result["reason"] = ("no declared channel maps to a delivery channel with a known exit path"
                            if channels else
                            "no declared channels (tbl_use_case_channel_rule has no row for this "
                            "use case), so the exit path cannot be built")
    return result
