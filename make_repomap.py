#!/usr/bin/env python3
"""Generate index/REPOMAP.md from the local mirror and dependency graph."""
import argparse
import collections
import csv
import os
import re

from retriever import config

_SKIP = {".git", "target", "build", "node_modules", ".codegraph"}
_ENTRY_RE = re.compile(r"@(RestController|Controller|JmsListener|KafkaListener|RabbitListener|SpringBootApplication)\b")
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def _iter_repos(mirror):
    if not os.path.isdir(mirror):
        return []
    return [
        name
        for name in sorted(os.listdir(mirror))
        if os.path.isdir(os.path.join(mirror, name)) and not name.startswith(".")
    ]


def _iter_files(root, suffixes):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP]
        for name in filenames:
            if name.lower().endswith(suffixes):
                yield os.path.join(dirpath, name)


def _rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _readme_purpose(repo_dir):
    for name in os.listdir(repo_dir):
        if name.lower().startswith("readme"):
            path = os.path.join(repo_dir, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for index, line in enumerate(handle, 1):
                        text = line.lstrip("\ufeff").strip(" #\t\r\n")
                        if text:
                            return text[:180], f"{name}:{index}"
            except OSError:
                continue
    return "No README summary found; infer from package and entry points.", ""


def _top_package(repo_dir, repo_name):
    counts = collections.Counter()
    evidence = {}
    for path in _iter_files(repo_dir, (".java",)):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read(4000).lstrip("\ufeff")
        except OSError:
            continue
        match = _PACKAGE_RE.search(text)
        if not match:
            continue
        package = match.group(1)
        line_no = text[:match.start()].count("\n") + 1
        counts[package] += 1
        evidence.setdefault(package, f"{_rel(path, os.path.dirname(repo_dir))}:{line_no}")
    if not counts:
        return "", ""
    package, _ = counts.most_common(1)[0]
    return package, evidence.get(package, "")


def _entry_points(repo_dir, repo_name, limit=4):
    out = []
    mirror = os.path.dirname(repo_dir)
    for path in _iter_files(repo_dir, (".java",)):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    match = _ENTRY_RE.search(line)
                    if match:
                        out.append(f"{_rel(path, mirror)}:{line_no} @{match.group(1)}")
                        break
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


def _load_edges(path):
    deps = collections.defaultdict(set)
    rev = collections.defaultdict(set)
    if not os.path.exists(path):
        return deps, rev
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            src = (row.get("from_repo") or "").strip()
            dst = (row.get("to_repo") or "").strip()
            if src and dst:
                deps[src].add(dst)
                rev[dst].add(src)
    return deps, rev


def _short_list(values, limit=6):
    values = sorted(values)
    if not values:
        return "none known"
    shown = ", ".join(values[:limit])
    return shown + (f", +{len(values) - limit} more" if len(values) > limit else "")


def build_repomap(mirror, edges_path):
    deps, rev = _load_edges(edges_path)
    lines = [
        "# Repository Map",
        "",
        "Generated from the local read-only mirror and recon dependency graph.",
        "",
    ]
    for repo in _iter_repos(mirror):
        repo_dir = os.path.join(mirror, repo)
        purpose, purpose_ref = _readme_purpose(repo_dir)
        package, package_ref = _top_package(repo_dir, repo)
        entries = _entry_points(repo_dir, repo)
        lines.extend(
            [
                f"## {repo}",
                f"- Purpose: {purpose}" + (f" ({repo}/{purpose_ref})" if purpose_ref else ""),
                f"- Top package: {package or 'unknown'}" + (f" ({package_ref})" if package_ref else ""),
                f"- Entry points: {('; '.join(entries)) if entries else 'none found'}",
                f"- Depends on: {_short_list(deps.get(repo, set()))}",
                f"- Used by: {_short_list(rev.get(repo, set()))}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate index/REPOMAP.md.")
    parser.add_argument("--mirror", default=config.MIRROR)
    parser.add_argument("--edges", default=config.EDGES_CSV)
    parser.add_argument("--out", default=os.path.join(config.INDEX_DIR, "REPOMAP.md"))
    args = parser.parse_args(argv)

    if not os.path.isdir(args.mirror):
        print(f"missing mirror: {args.mirror}")
        return 1
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    text = build_repomap(args.mirror, args.edges)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
