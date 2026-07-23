#!/usr/bin/env python3
"""Refresh generated indexes and record freshness metadata."""
import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from retriever import config, usecase_master


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


def _load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _edge_stats(edges_csv):
    repos, edges = set(), 0
    try:
        with open(edges_csv, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                source = (row.get("from_repo") or "").strip()
                target = (row.get("to_repo") or "").strip()
                if source:
                    repos.add(source)
                if target:
                    repos.add(target)
                if source and target:
                    edges += 1
    except FileNotFoundError:
        return None
    return {"repos": len(repos), "edges": edges}


def write_summary(index_dir, recon_dir, generated_at, path):
    """Compact, photo-able headline coverage — one file the box relays back after a refresh."""
    import make_repo_tags  # local import: reuse its coverage_rows on the generated repo_tags.json

    lines = ["# Full-repo artifact refresh — summary", "", f"_generated: {generated_at}_", ""]

    edges = _edge_stats(os.path.join(recon_dir, "internal_edges.csv"))
    if edges:
        lines.append(f"- **maven graph:** {edges['repos']} repos, {edges['edges']} edges")

    tags = _load_json(os.path.join(index_dir, "repo_tags.json"))
    if isinstance(tags, dict) and tags:
        rows = dict(make_repo_tags.coverage_rows(tags))
        lines.append(
            f"- **repo tags:** total {rows.get('repos_total')}, "
            f"channel_unknown {rows.get('channel_unknown')}, "
            f"serves_channel_set {rows.get('serves_channel_set')}, "
            f"channel_true_dark {rows.get('channel_true_dark')}, "
            f"mdc_common {rows.get('mdc_common_set')}, "
            f"time_critical {rows.get('time_critical_set')}"
        )

    topo = _load_json(os.path.join(index_dir, "delivery_topology.json"))
    if isinstance(topo, dict) and topo:
        channels = [key for key in topo if key not in {"by_repo", "unassigned"}]
        by_repo = topo.get("by_repo") or {}
        jobs = sum(1 for meta in by_repo.values() if meta.get("kind") == "delivery-job")
        apis = sum(1 for meta in by_repo.values() if meta.get("kind") == "outbound-api")
        lines.append(f"- **delivery topology:** {len(channels)} channels, {jobs} delivery-jobs, {apis} outbound-apis")

    arch = _load_json(os.path.join(index_dir, "arch_map.json"))
    if isinstance(arch, dict) and isinstance(arch.get("coverage"), dict):
        cov = arch["coverage"]
        lines.append(
            f"- **arch map:** nodes bound {cov.get('nodes_bound')} / "
            f"empty {cov.get('nodes_empty')} (of {cov.get('nodes_total')})"
        )

    recon = _load_json(os.path.join(index_dir, "reports", "TAG_RECONCILE.json"))
    if isinstance(recon, dict) and isinstance(recon.get("summary"), dict):
        summary = recon["summary"]
        lines.append(
            f"- **MDC reconcile:** confirmations {summary.get('confirmations')}, "
            f"mismatches {summary.get('mismatches')}, "
            f"explained_unknowns {summary.get('explained_unknowns')}, "
            f"true_dark {summary.get('true_dark')}"
        )

    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return text


def write_usecase_quality(index_dir):
    """Additive Tier 0 step: usecase_master.quality_report() -> index/reports/USECASE_QUALITY.{md,json}.
    Missing master snapshot -> a clean 'absent' step (returncode 0), never a failure — same
    honesty rule as every other snapshot-backed step here."""
    report = usecase_master.quality_report()
    reports_dir = os.path.join(index_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    md_path = os.path.join(reports_dir, "USECASE_QUALITY.md")
    json_path = os.path.join(reports_dir, "USECASE_QUALITY.json")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(usecase_master.render_quality_markdown(report))
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    if not report.get("available"):
        return {"cmd": ["usecase_master.quality_report"], "returncode": 0,
                "skipped": report.get("note") or "master snapshot absent"}
    return {"cmd": ["usecase_master.quality_report"], "returncode": 0, "wrote": [md_path, json_path]}


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
    edges_csv = os.path.join(recon_dir, "internal_edges.csv")
    repos_txt = os.path.join(recon_dir, "repos.txt")
    mdc_json = os.path.join(index_dir, "repo_tags.mdc.json")
    msg_channels_json = os.path.join(index_dir, "message_channels.json")
    repo_tags_json = os.path.join(index_dir, "repo_tags.json")
    delivery_json = os.path.join(index_dir, "delivery_topology.json")
    reconcile_md = os.path.join(index_dir, "reports", "TAG_RECONCILE.md")
    reconcile_json = os.path.join(index_dir, "reports", "TAG_RECONCILE.json")

    if os.path.isdir(mirror):
        report["steps"].append(_run([py, "recon_maven_graph.py", mirror, recon_dir], root))
        report["steps"].append(
            _run([py, "make_repomap.py", "--mirror", mirror, "--edges", edges_csv,
                  "--out", os.path.join(index_dir, "REPOMAP.md")], root)
        )
    else:
        report["steps"].append({"cmd": ["mirror scan"], "returncode": 1, "error": f"missing mirror {mirror}"})

    # Business-metadata overlay from the MDC sheet (additive; never clobbers structural tags).
    if os.path.exists(config.MDC_SHEET_XLSX):
        report["steps"].append(_run([py, "enrich_repo_tags.py", "--out", mdc_json], root))
    else:
        report["steps"].append(
            {"cmd": ["enrich_repo_tags.py"], "returncode": 0, "skipped": f"missing MDC sheet {config.MDC_SHEET_XLSX}"}
        )

    # Async message wiring (repo -> topic/queue -> channel) from source; feeds msg_channels into
    # make_repo_tags below so messaging-only repos get a channel Maven blast-radius can't reach.
    if os.path.isdir(mirror):
        report["steps"].append(
            _run([py, "make_message_map.py", "--mirror", mirror,
                  "--edges-out", os.path.join(index_dir, "message_edges.csv"),
                  "--channels-out", msg_channels_json], root)
        )
    else:
        report["steps"].append(
            {"cmd": ["make_message_map.py"], "returncode": 0, "skipped": f"missing mirror {mirror}"}
        )

    # Repo tags + serves_channels over the full active mirror, then the delivery topology.
    # RUNBOOK-50 made the curated mirror root (and therefore repos.txt) the canonical 460-repo
    # universe; pass it explicitly so edge-less infra/tooling repos do not disappear on refresh.
    # The FROZEN index/bundles.json is still read only for existing CodeGraph bundle assignments.
    if os.path.exists(edges_csv):
        report["steps"].append(
            _run([py, "make_repo_tags.py", "--edges", edges_csv, "--mdc", mdc_json,
                  "--msg-channels", msg_channels_json, "--repos-file", repos_txt,
                  "--out", repo_tags_json], root)
        )
        report["steps"].append(
            _run([py, "make_delivery_topology.py", "--edges", edges_csv,
                  "--repo-tags", repo_tags_json, "--out", delivery_json], root)
        )
        # Reconcile the sheet against the freshly-built tags (mismatches/confirmations for review).
        if os.path.exists(config.MDC_SHEET_XLSX):
            report["steps"].append(
                _run([py, "enrich_repo_tags.py", "--out", mdc_json, "--report",
                      "--tags", repo_tags_json, "--report-md", reconcile_md,
                      "--report-json", reconcile_json], root)
            )
    else:
        report["steps"].append(
            {"cmd": ["make_repo_tags.py"], "returncode": 1, "error": f"missing edges {edges_csv}"}
        )

    # NOTE: message_map_enrich.py (enum-symbol resolution) is superseded by make_message_map.py above —
    # RUNBOOK-26 confirmed this estate declares topics in config, not enum NAME("literal") constants, so
    # the enricher found nothing to resolve AND would rewrite the source-generated message_edges.csv.
    report["steps"].append(
        {"cmd": ["message_map_enrich.py"], "returncode": 0, "skipped": "superseded by make_message_map.py"}
    )

    # Re-bind the architecture map from the latest delivery_topology.json + repo_tags.json so the
    # clickable pipeline diagram never goes stale. The catalog is committed, so this always runs;
    # missing topology/tags simply yield honestly-empty nodes rather than a failure.
    arch_nodes = os.path.join(root, "static", "arch_nodes.json")
    if os.path.exists(arch_nodes):
        report["steps"].append(
            _run([py, "make_arch_map.py", "--catalog", arch_nodes, "--topology", delivery_json,
                  "--repo-tags", repo_tags_json, "--out", os.path.join(index_dir, "arch_map.json")], root)
        )
    else:
        report["steps"].append(
            {"cmd": ["make_arch_map.py"], "returncode": 0, "skipped": "missing static/arch_nodes.json"}
        )

    # Use Case master data quality (Tier 0) — additive, box-local export; absent snapshot is a
    # clean skip, not a failure.
    report["steps"].append(write_usecase_quality(index_dir))

    summary_path = os.path.join(index_dir, "reports", "REFRESH-SUMMARY.md")
    try:
        write_summary(index_dir, recon_dir, report["generated_at"], summary_path)
        report["summary_file"] = summary_path
    except Exception as error:  # noqa: BLE001
        report["summary_error"] = str(error)

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
    if report.get("summary_file"):
        print(f"wrote {report['summary_file']}")
    print(f"repos tracked: {len(report['repos'])}")
    print(f"steps failed: {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
