#!/usr/bin/env python3
"""Derive per-repo business tags from repo names plus optional bundle metadata."""
import argparse
import csv
import json
import os
from collections import defaultdict

from retriever import config
from retriever import graph

SYSTEM_PREFIXES = (
    ("mc-hk-hase-", "hase"),
    ("amet-mdc-hsbc-", "amet-mdc"),
    ("ai-", "ai"),
    ("aws-tf-", "infra"),
    ("shp-", "shp"),
    ("doris-", "data"),
)
CHANNEL_KEYWORDS = ("sms", "email", "push", "whatsapp", "wechat", "letter")
# "other"/"others" is a sheet bucket, NOT a delivery channel — it must never enter the
# authoritative `channel` field or `serves_channels` (it lives only in `channel_declared`).
NON_CHANNELS = frozenset({"other", "others", "unknown", "n/a", ""})
MODE_ALIASES = {"rt": "realtime", "bat": "batch", "batch": "batch"}
ROLE_SUFFIXES = ("job", "api", "core", "lib")


def load_repo_universe(edges_path):
    universe = set()
    try:
        with open(edges_path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                source = (row.get("from_repo") or "").strip()
                target = (row.get("to_repo") or "").strip()
                if source:
                    universe.add(source)
                if target:
                    universe.add(target)
    except FileNotFoundError:
        pass
    return universe


def load_pom_only_repos(path=None, inline=None):
    repos = set(inline or ())
    if not path:
        return repos
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line and not line.startswith("#"):
                repos.add(line)
    return repos


def load_repo_list(path):
    """Full scanned repo universe (recon_out/repos.txt), unioned in so edge-less repos still
    get a tag entry. Missing file → empty set (back-compatible with edge-only runs)."""
    repos = set()
    if not path:
        return repos
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for raw in handle:
                line = raw.strip()
                if line and not line.startswith("#"):
                    repos.add(line)
    except FileNotFoundError:
        pass
    return repos


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def load_bundle_map(path):
    payload = load_json(path)
    if not isinstance(payload, dict):
        return {}

    primary = {}
    secondary = defaultdict(set)
    for bundle, meta in payload.items():
        if not isinstance(meta, dict):
            continue
        for repo in meta.get("primary") or []:
            repo_name = str(repo).strip()
            if repo_name:
                primary[repo_name] = bundle
        for repo in meta.get("with_libs") or []:
            repo_name = str(repo).strip()
            if repo_name:
                secondary[repo_name].add(bundle)

    for repo, bundles in secondary.items():
        if repo not in primary and len(bundles) == 1:
            primary[repo] = next(iter(bundles))
    return primary


def load_bundle_universe(path):
    """All repos named in the frozen bundle plan (primary ∪ with_libs) — the canonical system
    universe (390), so every partitioned repo gets a tag entry even with no Maven edge, WITHOUT
    pulling in the extract's non-system extras (scanned dirs beyond the plan)."""
    payload = load_json(path)
    repos = set()
    if isinstance(payload, dict):
        for meta in payload.values():
            if not isinstance(meta, dict):
                continue
            for key in ("primary", "with_libs"):
                for repo in meta.get(key) or []:
                    name = str(repo).strip()
                    if name:
                        repos.add(name)
    return repos


def detect_system(repo):
    lowered = repo.lower()
    for prefix, system in SYSTEM_PREFIXES:
        if lowered.startswith(prefix):
            return system, lowered[len(prefix):]
    return "other", lowered


def detect_channels(repo):
    lowered = repo.lower()
    return [channel for channel in CHANNEL_KEYWORDS if channel in lowered]


def detect_mode(tokens):
    lowered = [token.lower() for token in tokens]
    if "rt" in lowered:
        return "realtime"
    if "bat" in lowered or "batch" in lowered:
        return "batch"
    if lowered and lowered[-1] in ROLE_SUFFIXES:
        return lowered[-1]
    return ""


def derive_tokens(repo):
    _system, remainder = detect_system(repo)
    tokens = [token for token in remainder.split("-") if token]
    if tokens and tokens[-1].lower() in ROLE_SUFFIXES:
        tokens = tokens[:-1]

    out = []
    for token in tokens:
        lowered = token.lower()
        if lowered in MODE_ALIASES or lowered in CHANNEL_KEYWORDS:
            continue
        out.append(lowered)
    return out


def derive_repo_tags(repo, bundle_map):
    system, remainder = detect_system(repo)
    tokens = [token for token in remainder.split("-") if token]
    return {
        "system": system,
        "channel": detect_channels(repo),
        "mode": detect_mode(tokens),
        "tokens": derive_tokens(repo),
        "bundle": bundle_map.get(repo, ""),
    }


def merge_override(derived, override):
    out = dict(derived)
    if not isinstance(override, dict):
        return out

    for field in ("system", "mode", "bundle"):
        if field in override:
            out[field] = str(override.get(field) or "").strip()
    if "tokens" in override:
        values = override.get("tokens") or []
        out["tokens"] = [str(value).strip() for value in values if str(value).strip()]
    if "channel" in override:
        # Keep only real delivery channels; a ["others"]/empty override must NOT clobber a
        # name-derived channel (guards against the stale bulk-"others" override file).
        real = [
            str(value).strip().lower()
            for value in (override.get("channel") or [])
            if str(value).strip().lower() not in NON_CHANNELS
        ]
        if real:
            out["channel"] = real
    return out


def merge_mdc(derived, mdc):
    """Add sheet metadata without ever replacing structural name-derived tags."""
    out = dict(derived)
    if not isinstance(mdc, dict):
        return out
    for field in (
        "mdc_common", "marketing_servicing", "time_critical", "business_line",
        "channel_declared", "mode_declared",
    ):
        if field in mdc:
            out[field] = mdc[field]
    return out


def serves_channels(repo, tags, edges_path, graph_data=None):
    affected = graph.impact(
        repo, transitive=True, edges_path=edges_path, graph_data=graph_data
    )["depended_on_by"]
    affected.append(repo)
    return sorted({
        channel
        for name in affected
        for channel in tags.get(name, {}).get("channel", [])
        if channel and channel.lower() not in NON_CHANNELS
    })


def build_repo_tags(args):
    universe = load_repo_universe(args.edges)
    bundle_repos = load_bundle_universe(args.bundles)
    repo_list = load_repo_list(args.repos_file)
    pom_only = load_pom_only_repos(args.pom_only_file, args.pom_only_repo)
    bundle_map = load_bundle_map(args.bundles)
    mdc = load_json(args.mdc)
    overrides = load_json(args.override)
    # Canonical universe = Maven edge endpoints ∪ the frozen bundle plan (the 390-repo system).
    # NOT all scanned dirs — the extract carries ~66 non-system extras we deliberately exclude.
    # `--repos-file` stays available as an explicit opt-in for a "tag every scanned dir" run.
    repos = sorted(universe | bundle_repos | repo_list | pom_only)

    payload = {
        repo: merge_mdc(derive_repo_tags(repo, bundle_map), mdc.get(repo.lower()))
        for repo in repos
    }
    for repo in repos:
        payload[repo] = merge_override(payload[repo], overrides.get(repo))
    dependency_graph = graph.load_dependency_graph(args.edges)
    for repo in repos:
        payload[repo]["serves_channels"] = serves_channels(
            repo, payload, args.edges, dependency_graph
        )
    return payload


def coverage_rows(payload):
    total = len(payload)
    system_set = sum(1 for meta in payload.values() if meta.get("system") and meta.get("system") != "other")
    channel_set = sum(1 for meta in payload.values() if meta.get("channel"))
    mode_set = sum(1 for meta in payload.values() if meta.get("mode"))
    system_other = sum(1 for meta in payload.values() if meta.get("system") == "other")
    channel_unknown = total - channel_set
    mode_unknown = total - mode_set
    serves_channel_set = sum(1 for meta in payload.values() if meta.get("serves_channels"))
    marketing_servicing_set = sum(1 for meta in payload.values() if meta.get("marketing_servicing"))
    time_critical_set = sum(1 for meta in payload.values() if meta.get("time_critical"))
    mdc_common_set = sum(1 for meta in payload.values() if meta.get("mdc_common"))
    channel_explained = sum(
        1
        for meta in payload.values()
        if not meta.get("channel")
        and (meta.get("mdc_common") or "other" in meta.get("channel_declared", []) or meta.get("serves_channels"))
    )
    channel_true_dark = channel_unknown - channel_explained
    return [
        ("repos_total", total),
        ("system_set", system_set),
        ("channel_set", channel_set),
        ("mode_set", mode_set),
        ("system_other", system_other),
        ("channel_unknown", channel_unknown),
        ("mode_unknown", mode_unknown),
        ("serves_channel_set", serves_channel_set),
        ("marketing_servicing_set", marketing_servicing_set),
        ("time_critical_set", time_critical_set),
        ("mdc_common_set", mdc_common_set),
        ("channel_explained", channel_explained),
        ("channel_true_dark", channel_true_dark),
    ]


def print_coverage(payload):
    rows = coverage_rows(payload)
    print("Repo tag coverage")
    print("")
    header = f"{'metric':18} {'count':>6}"
    print(header)
    print("-" * len(header))
    for label, count in rows:
        print(f"{label:18} {count:6d}")


def write_payload(payload, out_path):
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", default=config.EDGES_CSV)
    parser.add_argument(
        "--repos-file",
        default="",
        help="opt-in: a repo list (e.g. recon_out/repos.txt) unioned into the universe to tag "
             "every scanned dir, including non-system extras. Default off — the bundle plan is canonical.",
    )
    parser.add_argument("--bundles", default=config.BUNDLES_JSON)
    parser.add_argument("--override", default=config.REPO_TAGS_OVERRIDE_JSON)
    parser.add_argument("--mdc", default=config.REPO_TAGS_MDC_JSON)
    parser.add_argument("--out", default=config.REPO_TAGS_JSON)
    parser.add_argument("--pom-only-file", help="newline-delimited repo names to add to the universe")
    parser.add_argument(
        "--pom-only-repo",
        action="append",
        default=[],
        help="repeatable pom-only repo name to add to the universe",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    payload = build_repo_tags(args)
    write_payload(payload, args.out)
    print_coverage(payload)
    print("")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
