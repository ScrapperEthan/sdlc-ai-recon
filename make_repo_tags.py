#!/usr/bin/env python3
"""Derive per-repo business tags from repo names plus optional bundle metadata."""
import argparse
import csv
import json
import os
from collections import defaultdict

from retriever import config

SYSTEM_PREFIXES = (
    ("mc-hk-hase-", "hase"),
    ("amet-mdc-hsbc-", "amet-mdc"),
    ("ai-", "ai"),
    ("aws-tf-", "infra"),
    ("shp-", "shp"),
    ("doris-", "data"),
)
CHANNEL_KEYWORDS = ("sms", "email", "push", "whatsapp", "wechat", "letter")
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
    for field in ("channel", "tokens"):
        if field in override:
            values = override.get(field) or []
            out[field] = [str(value).strip() for value in values if str(value).strip()]
    return out


def build_repo_tags(args):
    universe = load_repo_universe(args.edges)
    pom_only = load_pom_only_repos(args.pom_only_file, args.pom_only_repo)
    bundle_map = load_bundle_map(args.bundles)
    overrides = load_json(args.override)
    repos = sorted(universe | pom_only)

    payload = {}
    for repo in repos:
        payload[repo] = merge_override(derive_repo_tags(repo, bundle_map), overrides.get(repo))
    return payload


def coverage_rows(payload):
    total = len(payload)
    system_set = sum(1 for meta in payload.values() if meta.get("system") and meta.get("system") != "other")
    channel_set = sum(1 for meta in payload.values() if meta.get("channel"))
    mode_set = sum(1 for meta in payload.values() if meta.get("mode"))
    system_other = sum(1 for meta in payload.values() if meta.get("system") == "other")
    channel_unknown = total - channel_set
    mode_unknown = total - mode_set
    return [
        ("repos_total", total),
        ("system_set", system_set),
        ("channel_set", channel_set),
        ("mode_set", mode_set),
        ("system_other", system_other),
        ("channel_unknown", channel_unknown),
        ("mode_unknown", mode_unknown),
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
    parser.add_argument("--bundles", default=config.BUNDLES_JSON)
    parser.add_argument("--override", default=config.REPO_TAGS_OVERRIDE_JSON)
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
