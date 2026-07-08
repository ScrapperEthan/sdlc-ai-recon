#!/usr/bin/env python3
"""Compute reviewable CodeGraph bundle partitions from the full dependency graph.

This is a planner only:
- stdlib only
- read-only over recon_out/internal_edges.csv
- never touches mirror/
- does not build CodeGraph

It emits index/bundles.json plus a review table so we can tune merge thresholds
and any manual overrides before cloning or indexing per bundle.
"""
import argparse
import csv
import json
import os
from collections import defaultdict

import group

PREFIX = "mc-hk-hase-"
ROLE_SUFFIXES = ("-api", "-core", "-job", "-svc", "-lib")
SPECIAL_BUNDLES = {"platform-core", "tracking"}
DEFAULT_EST_MIB_PER_REPO = 10.5


def load_repo_universe(edges_path):
    universe = set()
    fwd = defaultdict(set)
    with open(edges_path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            source = (row.get("from_repo") or "").strip()
            target = (row.get("to_repo") or "").strip()
            if source:
                universe.add(source)
            if target:
                universe.add(target)
            if source and target:
                fwd[source].add(target)
    return universe, fwd


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


def repo_stem(repo):
    stem = repo.strip()
    if stem.startswith(PREFIX):
        stem = stem[len(PREFIX):]
    for suffix in ROLE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.strip("-")


def primary_bundle_key(repo):
    stem = repo_stem(repo)
    parts = [part for part in stem.split("-") if part]
    if not parts:
        return "misc-unknown"
    leading = parts[0]
    if leading == "api":
        return "platform-core"
    if leading in {"svc", "ssvc"} and len(parts) >= 2:
        return f"{leading}-{parts[1]}"
    return leading


def build_misc_groups(bundle_to_repos, merge_min):
    if merge_min <= 0:
        return {}

    small_keys = sorted(
        key
        for key, repos in bundle_to_repos.items()
        if len(repos) < merge_min and key not in SPECIAL_BUNDLES
    )
    if not small_keys:
        return {}

    groups = []
    current = []
    current_count = 0
    for key in small_keys:
        repos = sorted(bundle_to_repos[key])
        current.append((key, repos))
        current_count += len(repos)
        if current_count >= merge_min:
            groups.append(current)
            current = []
            current_count = 0
    if current:
        if groups:
            groups[-1].extend(current)
        else:
            groups.append(current)

    out = {}
    for chunk in groups:
        first = chunk[0][0]
        last = chunk[-1][0]
        name = f"misc-{first}" if first == last else f"misc-{first}-to-{last}"
        for key, _repos in chunk:
            out[key] = name
    return out


def assign_primary_bundles(universe, pom_only_repos=None, merge_min=8):
    pom_only = set(pom_only_repos or ())
    preliminary = defaultdict(set)
    for repo in sorted(universe | pom_only):
        key = "platform-core" if repo in pom_only else primary_bundle_key(repo)
        preliminary[key].add(repo)

    misc_map = build_misc_groups(preliminary, merge_min)
    final = defaultdict(set)
    owner = {}
    for key, repos in preliminary.items():
        bundle = misc_map.get(key, key)
        for repo in repos:
            if repo in owner:
                raise AssertionError(f"repo assigned twice: {repo}")
            owner[repo] = bundle
            final[bundle].add(repo)

    missing = sorted((universe | pom_only) - set(owner))
    if missing:
        raise AssertionError(f"unassigned repos: {missing}")
    return final, owner


def tracking_repos(universe):
    return {repo for repo in universe if "tracking" in repo.lower()}


def expand_with_downstream_closure(primary_repos, fwd):
    expanded = set()
    for repo in primary_repos:
        expanded |= group.bfs_down(fwd, repo, None)
    return expanded or set(primary_repos)


def build_bundle_payload(
    primary_bundles,
    fwd,
    est_mib_per_repo=DEFAULT_EST_MIB_PER_REPO,
    include_tracking=True,
):
    bundle_to_primary = {name: set(repos) for name, repos in primary_bundles.items()}
    if include_tracking:
        tracked = tracking_repos({repo for repos in primary_bundles.values() for repo in repos})
        if tracked:
            bundle_to_primary.setdefault("tracking", set()).update(tracked)

    payload = {}
    for bundle, primary in sorted(bundle_to_primary.items()):
        with_libs = sorted(expand_with_downstream_closure(primary, fwd))
        payload[bundle] = {
            "primary": sorted(primary),
            "with_libs": with_libs,
            "primary_count": len(primary),
            "total_count": len(with_libs),
            "est_codegraph_mib": round(len(with_libs) * est_mib_per_repo),
        }
    return payload


def review_rows(payload, max_repos=60, max_mib=600):
    rows = []
    for bundle, meta in sorted(payload.items(), key=lambda item: (-item[1]["total_count"], item[0])):
        flags = []
        if meta["total_count"] > max_repos:
            flags.append(f"repos>{max_repos}")
        if meta["est_codegraph_mib"] > max_mib:
            flags.append(f"mib>{max_mib}")
        rows.append(
            {
                "bundle": bundle,
                "primary_count": meta["primary_count"],
                "total_count": meta["total_count"],
                "est_codegraph_mib": meta["est_codegraph_mib"],
                "flags": flags,
            }
        )
    return rows


def print_review_table(rows, coverage, total_repos):
    print(f"Primary coverage: {coverage}/{total_repos} repos")
    print("")
    header = f"{'bundle':30} {'primary':>7} {'total':>7} {'est_mib':>8}  flags"
    print(header)
    print("-" * len(header))
    for row in rows:
        flags = ",".join(row["flags"]) if row["flags"] else "-"
        print(
            f"{row['bundle'][:30]:30} "
            f"{row['primary_count']:7d} "
            f"{row['total_count']:7d} "
            f"{row['est_codegraph_mib']:8d}  "
            f"{flags}"
        )


def write_payload(payload, out_path):
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def build_plan(args):
    universe, fwd = load_repo_universe(args.edges)
    pom_only = load_pom_only_repos(args.pom_only_file, args.pom_only_repo)
    primary_bundles, owners = assign_primary_bundles(universe, pom_only, args.merge_min)
    payload = build_bundle_payload(primary_bundles, fwd, args.est_mib_per_repo, include_tracking=True)
    rows = review_rows(payload, args.max_repos, args.max_mib)
    coverage = len(owners)
    total_repos = len(universe | pom_only)
    if coverage != total_repos:
        raise AssertionError(f"coverage mismatch: {coverage} != {total_repos}")
    return payload, rows, coverage, total_repos


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", default=os.path.join("recon_out", "internal_edges.csv"))
    parser.add_argument("--out", default=os.path.join("index", "bundles.json"))
    parser.add_argument("--pom-only-file", help="newline-delimited repo names to add to the universe")
    parser.add_argument(
        "--pom-only-repo",
        action="append",
        default=[],
        help="repeatable pom-only repo name to add to the universe",
    )
    parser.add_argument("--merge-min", type=int, default=8)
    parser.add_argument("--max-repos", type=int, default=60)
    parser.add_argument("--max-mib", type=int, default=600)
    parser.add_argument("--est-mib-per-repo", type=float, default=DEFAULT_EST_MIB_PER_REPO)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    payload, rows, coverage, total_repos = build_plan(args)
    write_payload(payload, args.out)
    print_review_table(rows, coverage, total_repos)
    print("")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
