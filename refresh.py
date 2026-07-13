#!/usr/bin/env python3
"""Refresh generated indexes and record freshness metadata."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from retriever import config


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(cmd, cwd):
    started = _now()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        return {
            "cmd": cmd,
            "started_at": started,
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
    except Exception as error:  # noqa: BLE001
        return {"cmd": cmd, "started_at": started, "returncode": -1, "error": str(error)}


def _git(repo_dir, args):
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _repo_state(mirror, fetch=False):
    repos = []
    if not os.path.isdir(mirror):
        return repos
    for name in sorted(os.listdir(mirror)):
        repo_dir = os.path.join(mirror, name)
        if not os.path.isdir(repo_dir) or not os.path.isdir(os.path.join(repo_dir, ".git")):
            continue
        fetch_result = None
        if fetch:
            fetch_result = _run(["git", "fetch", "--all", "--prune", "--depth=1"], repo_dir)
        repos.append(
            {
                "name": name,
                "branch": _git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"]),
                "commit": _git(repo_dir, ["rev-parse", "HEAD"]),
                "dirty": bool(_git(repo_dir, ["status", "--porcelain"])),
                "fetched": fetch_result,
            }
        )
    return repos


def refresh(fetch=False, root=None, mirror=None, index_dir=None, recon_dir=None):
    root = root or os.getcwd()
    mirror = mirror or config.MIRROR
    index_dir = index_dir or config.INDEX_DIR
    recon_dir = recon_dir or config.RECON_DIR
    os.makedirs(index_dir, exist_ok=True)
    os.makedirs(recon_dir, exist_ok=True)

    report = {
        "generated_at": _now(),
        "root": root,
        "mirror": mirror,
        "index_dir": index_dir,
        "recon_dir": recon_dir,
        "fetch_enabled": fetch,
        "repos": _repo_state(mirror, fetch),
        "steps": [],
    }

    py = sys.executable
    if os.path.isdir(mirror):
        edges_csv = os.path.join(recon_dir, "internal_edges.csv")
        report["steps"].append(_run([py, "recon_maven_graph.py", mirror, recon_dir], root))
        report["steps"].append(
            _run([py, "make_repomap.py", "--mirror", mirror, "--edges", edges_csv,
                  "--out", os.path.join(index_dir, "REPOMAP.md")], root)
        )
    else:
        report["steps"].append({"cmd": ["mirror scan"], "returncode": 1, "error": f"missing mirror {mirror}"})

    message_edges = os.path.join(index_dir, "message_edges.csv")
    if os.path.exists(message_edges) and os.path.isdir(mirror):
        report["steps"].append(_run([py, "message_map_enrich.py", "--edges", message_edges, "--mirror", mirror], root))
    else:
        report["steps"].append(
            {"cmd": ["message_map_enrich.py"], "returncode": 0, "skipped": "missing message_edges.csv or mirror"}
        )

    # Re-bind the architecture map from the latest delivery_topology.json + repo_tags.json so the
    # clickable pipeline diagram never goes stale. The catalog is committed, so this always runs;
    # missing topology/tags simply yield honestly-empty nodes rather than a failure.
    arch_nodes = os.path.join(root, "static", "arch_nodes.json")
    if os.path.exists(arch_nodes):
        report["steps"].append(_run([py, "make_arch_map.py", "--catalog", arch_nodes], root))
    else:
        report["steps"].append(
            {"cmd": ["make_arch_map.py"], "returncode": 0, "skipped": "missing static/arch_nodes.json"}
        )

    status_path = os.path.join(index_dir, "last_indexed.json")
    with open(status_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh local generated indexes.")
    parser.add_argument("--fetch", action="store_true", help="run git fetch in mirror repos before rebuilding")
    parser.add_argument("--mirror", default=config.MIRROR)
    parser.add_argument("--index-dir", default=config.INDEX_DIR)
    parser.add_argument("--recon-dir", default=config.RECON_DIR)
    args = parser.parse_args(argv)

    report = refresh(fetch=args.fetch, mirror=args.mirror, index_dir=args.index_dir, recon_dir=args.recon_dir)
    failed = [step for step in report["steps"] if step.get("returncode")]
    print(f"wrote {os.path.join(args.index_dir, 'last_indexed.json')}")
    print(f"repos tracked: {len(report['repos'])}")
    print(f"steps failed: {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
