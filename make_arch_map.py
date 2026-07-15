#!/usr/bin/env python3
"""Bind the static architecture-map node catalog to real repos (read-only inputs).

Resolves each diagram node in ``static/arch_nodes.json`` to the repos behind it, using
data we already generate: ``index/delivery_topology.json`` (channel+vendor -> deli-job /
outbound-api repos) and ``index/repo_tags.json`` (repos whose channel / name matches a
node). A small hand ``index/arch_map.override.json`` covers nodes whose names don't reveal
their repos (for example HARO, CN Gateway). Emits ``index/arch_map.json`` for ``/arch-map``.
"""
import argparse
import json
import os
from datetime import datetime, timezone

from retriever import config

# "other"/"others" is a sheet bucket, never a delivery channel — mirror make_repo_tags so a
# node's serves_channels rollup can never surface it.
NON_CHANNELS = frozenset({"other", "others", "unknown", "n/a", ""})


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def load_catalog(path):
    """Return the list of node dicts from the catalog file (or an empty list)."""
    data = load_json(path)
    nodes = data.get("nodes") if isinstance(data, dict) else data
    return [node for node in (nodes or []) if isinstance(node, dict) and node.get("id")]


def _clean_channels(values):
    return sorted({
        str(value).strip().lower()
        for value in (values or [])
        if str(value).strip().lower() not in NON_CHANNELS
    })


def _channel_groups(topology, channel):
    vendors = topology.get(channel) if isinstance(topology, dict) else None
    if not isinstance(vendors, dict):
        return {}
    return {vendor: group for vendor, group in vendors.items() if isinstance(group, dict)}


def bind_node(node, topology, tags):
    """Resolve one node to the set of real repos behind it (see module docstring for rules)."""
    role = (node.get("role") or "").strip().lower()
    channel = (node.get("channel") or "").strip().lower()
    vendor = (node.get("vendor") or "").strip().lower()
    repos = set()
    source = None

    if role == "delivery-job" and channel:
        for group in _channel_groups(topology, channel).values():
            repos.update(job["repo"] for job in group.get("delivery_jobs") or [] if job.get("repo"))
        source = "delivery_topology"
    elif role == "outbound-api" and channel:
        for name, group in _channel_groups(topology, channel).items():
            if vendor and name.lower() != vendor:
                continue
            repos.update(api["repo"] for api in group.get("outbound_apis") or [] if api.get("repo"))
        source = "delivery_topology"
    elif role == "topic" and channel:
        # Topics are usually Kafka topics, not repos; bind only repos that both carry the
        # channel tag and look like a topic/channel component. Often thin — that's honest.
        for repo, meta in tags.items():
            if channel in (meta.get("channel") or []) and "topic" in repo.lower():
                repos.add(repo)
        source = "repo_tags"
    elif role in ("ingress", "decision"):
        for repo in tags:
            if role in repo.lower():
                repos.add(repo)
        source = "repo_tags"
    elif role == "external" and channel:
        # Vendor endpoints / terminals (APNs·FCM, CSL SMSC, ProofPoint, …) are third-party, not our
        # code — but the repos that INTEGRATE with them (deliver on this channel, to this vendor when
        # known) ARE ours. Bind those, so the node maps to real repos instead of an empty terminal.
        for name, group in _channel_groups(topology, channel).items():
            if vendor and name.lower() != vendor:
                continue
            repos.update(job["repo"] for job in group.get("delivery_jobs") or [] if job.get("repo"))
            repos.update(api["repo"] for api in group.get("outbound_apis") or [] if api.get("repo"))
        source = "delivery_topology"

    return repos, (source if repos else None)


def rollup_serves_channels(repos, tags):
    channels = set()
    for repo in repos:
        meta = tags.get(repo) or {}
        channels.update(meta.get("serves_channels") or [])
        channels.update(meta.get("channel") or [])
    return _clean_channels(channels)


def _apply_override(entry, override):
    if not isinstance(override, dict):
        return entry
    extra = [str(repo).strip() for repo in (override.get("repos") or []) if str(repo).strip()]
    if extra:
        entry["repos"] = sorted(set(entry["repos"]) | set(extra))
        entry["sources"] = sorted(set(entry["sources"]) | {"override"})
    if override.get("serves_channels"):
        entry["serves_channels"] = _clean_channels(
            set(entry.get("serves_channels") or []) | set(override["serves_channels"])
        )
    if override.get("note"):
        entry["note"] = override["note"]
    return entry


def build_map(catalog, topology, tags, override=None):
    nodes = catalog.get("nodes") if isinstance(catalog, dict) else catalog
    override = override or {}
    result = {}
    for node in nodes or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        repos, source = bind_node(node, topology, tags)
        entry = {
            "label": node.get("label") or node["id"],
            "role": node.get("role") or "",
            "channel": (node.get("channel") or "") or None,
            "vendor": (node.get("vendor") or "") or None,
            "repos": sorted(repos),
            "sources": [source] if source else [],
        }
        if node.get("note"):
            entry["note"] = node["note"]
        entry = _apply_override(entry, override.get(node["id"]))
        entry["repo_count"] = len(entry["repos"])
        entry["serves_channels"] = _clean_channels(
            set(entry.get("serves_channels") or []) | set(rollup_serves_channels(entry["repos"], tags))
        )
        entry["bound"] = bool(entry["repos"])
        result[node["id"]] = entry
    return result


def coverage(nodes):
    total = len(nodes)
    bound = sum(1 for entry in nodes.values() if entry.get("bound"))
    return [("nodes_total", total), ("nodes_bound", bound), ("nodes_empty", total - bound)]


def build_payload(catalog_path, topology_path, tags_path, override_path):
    catalog = load_json(catalog_path)
    topology = load_json(topology_path)
    tags = load_json(tags_path)
    override = load_json(override_path)
    nodes = build_map(catalog, topology, tags, override)
    return {
        "generated_at": _now(),
        "repo_total": len(tags) if isinstance(tags, dict) else 0,
        "nodes": nodes,
        "coverage": dict(coverage(nodes)),
    }


def write_payload(payload, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=config.ARCH_NODES_JSON)
    parser.add_argument("--topology", default=config.DELIVERY_TOPOLOGY_JSON)
    parser.add_argument("--repo-tags", default=config.REPO_TAGS_JSON)
    parser.add_argument("--override", default=config.ARCH_MAP_OVERRIDE_JSON)
    parser.add_argument("--out", default=config.ARCH_MAP_JSON)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    payload = build_payload(args.catalog, args.topology, args.repo_tags, args.override)
    write_payload(payload, args.out)
    rows = dict(coverage(payload["nodes"]))
    print("Architecture map coverage")
    print(f"nodes bound / empty: {rows['nodes_bound']} / {rows['nodes_empty']} (of {rows['nodes_total']})")
    empty = sorted(node_id for node_id, entry in payload["nodes"].items() if not entry.get("bound"))
    if empty:
        print("Unbound nodes: " + ", ".join(empty))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
