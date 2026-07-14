#!/usr/bin/env python3
"""Reproducible per-bundle CodeGraph builder (turns the RUNBOOK-16 recipe into one command).

For each bundle in ``index/bundles.json`` this stages the bundle's *present* repos under a real
directory tree, ``git init``s that staging root, then runs ``codegraph init .`` to build the
index at ``<staging_root>/.codegraph/``. It writes a manifest to ``index/codegraph_build.json``.

Why staging (not point CodeGraph at ``mirror/`` directly): the CLI takes **one** project root,
has no ``--out``/repo-list/multi-root, and does **not** traverse Windows junctions — so a bundle
must be materialized as a copied tree. The repo-root ``.gitignore`` would otherwise hide a staging
``repos/``, so we ``git init`` the staging root to scope ignore rules locally.

stdlib only, read-only over ``mirror/`` (copies out, never writes in), additive. CodeGraph needs
its elevated local mode for the SQLite writes. ``mirror/`` currently holds only a subset of the
~390 repos, so most bundles are partial — this builds what is present and reports the rest.

  python build_codegraph.py --dry-run          # print the present/total plan, build nothing
  python build_codegraph.py                     # build every buildable bundle (run elevated)
  python build_codegraph.py --only ingress      # build a single bundle
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone

from retriever import config

# Build value first, then the long tail (docs/DOMAIN-PARTITION-PLAN-zh.md §6).
PLAN_ORDER = ("ingress", "tracking", "platform-core")
# Never copy these into the staging tree: VCS, prior indexes, build output, JS deps.
STAGE_IGNORE = shutil.ignore_patterns(".git", ".codegraph", "target", "build", "node_modules")


def load_bundles(path):
    with open(path, encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("bundles.json must contain an object of {bundle: {...}}")
    return data


def bundle_repos(meta):
    """primary ∪ with_libs for one bundle, order-stable (primary first, then extra libs)."""
    if isinstance(meta, dict):
        primary = list(meta.get("primary") or [])
        with_libs = list(meta.get("with_libs") or [])
    else:
        primary, with_libs = list(meta or []), []
    repos = list(primary)
    for repo in with_libs:
        if repo not in repos:
            repos.append(repo)
    return repos


def _order_key(name):
    """PLAN_ORDER bundles first (in that order), then the rest alphabetically."""
    if name in PLAN_ORDER:
        return (0, PLAN_ORDER.index(name), name)
    return (1, 0, name)


def plan(bundles, mirror, only=None):
    """Pure: split each bundle's repos into present/missing under ``mirror/``, in build order.

    Returns a list of ``{bundle, repos, present, missing, present_count, missing_count}``.
    """
    rows = []
    for name in sorted(bundles, key=_order_key):
        if only and name != only:
            continue
        repos = bundle_repos(bundles[name])
        present, missing = [], []
        for repo in repos:
            (present if os.path.isdir(os.path.join(mirror, repo)) else missing).append(repo)
        rows.append(
            {
                "bundle": name,
                "repos": repos,
                "present": present,
                "missing": missing,
                "present_count": len(present),
                "missing_count": len(missing),
            }
        )
    return rows


def _force_remove(path):
    """rmtree that clears read-only bits (Windows git objects), tolerant of old/new Python."""
    if not os.path.isdir(path):
        return

    def handler(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    try:
        shutil.rmtree(path, onexc=handler)  # Python 3.12+
    except TypeError:  # pragma: no cover - older interpreters
        shutil.rmtree(path, onerror=handler)


def stage_bundle(name, present_repos, mirror, out_root):
    """Copy each present repo into ``out_root/<bundle>/<repo>/`` and ``git init`` the staging root.

    Re-runnable: a stale staging dir for the bundle is removed first. Returns the staging root.
    """
    staging_root = os.path.join(out_root, name)
    _force_remove(staging_root)
    os.makedirs(staging_root, exist_ok=True)
    for repo in present_repos:
        shutil.copytree(
            os.path.join(mirror, repo),
            os.path.join(staging_root, repo),
            ignore=STAGE_IGNORE,
        )
    # Fresh .git at the staging root so CodeGraph sees the copied sources (scopes ignore locally).
    subprocess.run(["git", "init", "-q"], cwd=staging_root, capture_output=True, text=True)
    return staging_root


def _db_mib(staging_root):
    db = os.path.join(staging_root, ".codegraph", "codegraph.db")
    try:
        return round(os.path.getsize(db) / (1024 * 1024), 1)
    except OSError:
        return None


def build_bundle(staging_root, codegraph_exe, timeout=None):
    """Run ``codegraph init .`` in the staging root. Returns (returncode, seconds, db_mib, error)."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            [codegraph_exe, "init", "."],
            cwd=staging_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception as error:  # noqa: BLE001 - report, don't crash the whole run
        return None, round(time.monotonic() - start, 1), None, str(error)
    seconds = round(time.monotonic() - start, 1)
    error = "" if result.returncode == 0 else (result.stderr or result.stdout or "")[:2000]
    return result.returncode, seconds, _db_mib(staging_root), error


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reconcile_from_disk(out_root):
    """Recover manifest entries from already-built index dirs (each `<bundle>/.codegraph/codegraph.db`)
    WITHOUT rebuilding — for when a completed build's manifest got clobbered by an `--only` run."""
    entries = []
    if not os.path.isdir(out_root):
        return entries
    for name in sorted(os.listdir(out_root)):
        root = os.path.join(out_root, name)
        db = os.path.join(root, ".codegraph", "codegraph.db")
        if os.path.isfile(db):
            entries.append({
                "bundle": name,
                "root": root,
                "returncode": 0,
                "db_mib": round(os.path.getsize(db) / (1024 * 1024), 1),
                "reconciled": True,
            })
    return entries


def merge_manifest_bundles(existing_path, new_entries):
    """Preserve prior per-bundle records so a subset run (``--only``) upserts instead of wiping
    the manifest. Routing reads this manifest, so a lone ``--only`` must NOT drop the other
    already-built bundles. A full run simply re-supplies every bundle and replaces them all."""
    by_name = {}
    try:
        with open(existing_path, encoding="utf-8") as handle:
            data = json.load(handle)
        for entry in (data.get("bundles") if isinstance(data, dict) else None) or []:
            name = entry.get("bundle") if isinstance(entry, dict) else None
            if name:
                by_name[name] = entry
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    for entry in new_entries:
        name = entry.get("bundle")
        if name:
            by_name[name] = entry
    return sorted(by_name.values(), key=lambda entry: entry.get("bundle", ""))


def write_manifest(manifest, path):
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def build_all(args):
    if getattr(args, "reconcile", False):
        entries = reconcile_from_disk(args.out_root)
        merged = merge_manifest_bundles(args.manifest, entries)
        write_manifest(
            {"generated_at": _now_iso(), "mirror": args.mirror,
             "out_root": args.out_root, "bundles": merged},
            args.manifest,
        )
        print(f"Reconciled {len(entries)} built bundle(s) from {args.out_root} "
              f"→ {args.manifest} ({len(merged)} bundles recorded)")
        return 0

    bundles = load_bundles(args.bundles)
    rows = plan(bundles, args.mirror, only=args.only)
    if args.only and not rows:
        raise SystemExit(f"bundle not found in {args.bundles}: {args.only}")

    codegraph_exe = None
    if not args.dry_run:
        codegraph_exe = shutil.which("codegraph")
        if not codegraph_exe:
            raise SystemExit(
                "codegraph not on PATH — open the elevated CodeGraph shell (or install it), "
                "or use --dry-run to preview the plan without building."
            )

    manifest_bundles = []
    for row in rows:
        name = row["bundle"]
        total = row["present_count"] + row["missing_count"]
        if row["present_count"] == 0:
            print(f"[skip] {name}: no repos in mirror ({row['missing_count']} missing)")
            manifest_bundles.append(
                {"bundle": name, "skipped": "no repos in mirror", "missing_count": row["missing_count"]}
            )
            continue

        print(f"[plan] {name}: {row['present_count']}/{total} repos present")
        if args.dry_run:
            continue

        staging_root = stage_bundle(name, row["present"], args.mirror, args.out_root)
        print(f"[build] {name}: codegraph init . ({row['present_count']} repos)")
        rc, seconds, db_mib, error = build_bundle(staging_root, codegraph_exe, args.timeout)
        entry = {
            "bundle": name,
            "root": staging_root,
            "staged_repos": row["present"],
            "staged_count": row["present_count"],
            "missing_count": row["missing_count"],
            "returncode": rc,
            "seconds": seconds,
            "db_mib": db_mib,
        }
        if error:
            entry["error"] = error
        print(
            f"[done] {name}: {'ok' if rc == 0 else f'rc={rc}'}, {seconds}s, "
            f"{db_mib if db_mib is not None else '?'} MiB"
        )
        manifest_bundles.append(entry)

    if args.dry_run:
        print("\n(dry run — nothing staged, built, or written)")
        return 0

    # Upsert into any existing manifest so an `--only` run never wipes the other built bundles
    # (routing resolves a bundle only if its entry is present here).
    merged = merge_manifest_bundles(args.manifest, manifest_bundles)
    write_manifest(
        {
            "generated_at": _now_iso(),
            "mirror": args.mirror,
            "out_root": args.out_root,
            "bundles": merged,
        },
        args.manifest,
    )
    print(f"\nWrote {args.manifest} ({len(merged)} bundles recorded)")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", help="build a single bundle by name")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the present/total plan and build nothing"
    )
    parser.add_argument(
        "--reconcile", action="store_true",
        help="rebuild the manifest from already-built index dirs under --out-root (no rebuild); "
             "use when a completed build's manifest was clobbered by an --only run",
    )
    parser.add_argument("--mirror", default=config.MIRROR)
    parser.add_argument("--out-root", default=config.CODEGRAPH_ROOT)
    parser.add_argument("--bundles", default=config.BUNDLES_JSON)
    parser.add_argument("--manifest", default=config.CODEGRAPH_BUILD_JSON)
    parser.add_argument(
        "--timeout", type=int, default=None, help="per-bundle codegraph build timeout (seconds)"
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.reconcile and not os.path.isfile(args.bundles):
        print(f"no bundles.json at {args.bundles} — run make_bundles.py first")
        return 2
    return build_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
