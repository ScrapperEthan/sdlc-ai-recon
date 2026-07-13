#!/usr/bin/env python3
"""Clone the full HASE estate into mirror/ so per-bundle CodeGraph can index every domain.

Resumable: skips repos already cloned, records failures to a manifest, and can retry just those
(``--retry-failed``). Reads the repo list from repo_tags.json / bundles.json / a ``--repos-file``
(whichever is available). Uses the machine's git credential helper — never embeds secrets. The URL
is fully templated so it works for https or ssh. Read-only except writing under mirror/.

Safe first run:  python clone_mirror.py --dry-run           # show plan + the URL pattern
                 python clone_mirror.py --limit 1           # prove one clone works, adjust --url-template if needed
Full run:        python clone_mirror.py                     # clone the rest (resumable)
Retry failures:  python clone_mirror.py --retry-failed index/mirror_clone.json
"""
import argparse
import json
import os
import shutil
import subprocess
import time

from retriever import config

DEFAULT_BASE = os.environ.get("SDLC_GIT_BASE", "https://alm-github.systems.uk.hsbc")
DEFAULT_ORG = os.environ.get("SDLC_GIT_ORG", "hase-mc")
DEFAULT_TEMPLATE = os.environ.get("SDLC_GIT_URL_TEMPLATE", "{base}/{org}/{repo}.git")


def _names_from_json(path):
    """Repo names from repo_tags.json (keys) or bundles.json (primary+with_libs)."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or not data:
        return []
    looks_like_bundles = all(
        isinstance(v, dict) and ("primary" in v or "with_libs" in v) for v in data.values()
    )
    names = set()
    if looks_like_bundles:
        for meta in data.values():
            for repo in (meta.get("primary") or []) + (meta.get("with_libs") or []):
                if str(repo).strip():
                    names.add(str(repo).strip())
    else:
        names.update(str(key).strip() for key in data if str(key).strip())
    return sorted(names)


def load_repo_names(args):
    if args.repos_file:
        with open(args.repos_file, encoding="utf-8") as handle:
            return sorted({ln.strip() for ln in handle if ln.strip() and not ln.startswith("#")})
    for path in (args.repo_tags, args.bundles):
        names = _names_from_json(path)
        if names:
            return names
    return []


def repo_url(name, base, org, template=DEFAULT_TEMPLATE):
    return template.format(base=base.rstrip("/"), org=org, repo=name)


def is_cloned(dest):
    return os.path.isdir(os.path.join(dest, ".git"))


def clone_one(name, mirror, url, timeout):
    dest = os.path.join(mirror, name)
    if is_cloned(dest):
        return "skip", ""
    if os.path.isdir(dest):  # partial/failed leftover — start clean
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(mirror, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", "--quiet", url, dest],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "failed", "timeout"
    if result.returncode == 0:
        return "cloned", ""
    shutil.rmtree(dest, ignore_errors=True)
    return "failed", (result.stderr or "").strip()[:300]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", default=config.MIRROR)
    parser.add_argument("--repo-tags", default=config.REPO_TAGS_JSON)
    parser.add_argument("--bundles", default=config.BUNDLES_JSON)
    parser.add_argument("--repos-file", help="newline-delimited repo names (overrides the json sources)")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--url-template", default=DEFAULT_TEMPLATE,
                        help="e.g. ssh: 'git@alm-github.systems.uk.hsbc:{org}/{repo}.git'")
    parser.add_argument("--limit", type=int, help="clone at most N (safe first test)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-failed", help="a previous manifest; clone only its failed repos")
    parser.add_argument("--manifest", default=os.path.join(config.INDEX_DIR, "mirror_clone.json"))
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.retry_failed:
        with open(args.retry_failed, encoding="utf-8") as handle:
            names = [e["repo"] for e in json.load(handle).get("failed", [])]
    else:
        names = load_repo_names(args)
    if not names:
        print("no repo names found (need repo_tags.json / bundles.json / --repos-file)")
        return 1

    present = [n for n in names if is_cloned(os.path.join(args.mirror, n))]
    todo = [n for n in names if n not in set(present)]
    if args.limit:
        todo = todo[: args.limit]
    print(f"repos: {len(names)}  already cloned: {len(present)}  to clone: {len(todo)}")
    print(f"url pattern: {repo_url('<repo>', args.base_url, args.org, args.url_template)}")
    if args.dry_run:
        for name in todo[:20]:
            print("  would clone", name)
        print("(dry run — nothing cloned)")
        return 0

    cloned, failed = [], []
    started = time.time()
    for index, name in enumerate(todo, 1):
        url = repo_url(name, args.base_url, args.org, args.url_template)
        status, err = clone_one(name, args.mirror, url, args.timeout)
        if status == "cloned":
            cloned.append(name)
        elif status == "failed":
            failed.append({"repo": name, "error": err})
        print(f"[{index}/{len(todo)}] {status:6} {name}" + (f"  {err}" if err else ""))

    manifest = {"cloned": cloned, "skipped": present, "failed": failed,
                "seconds": round(time.time() - started, 1)}
    os.makedirs(os.path.dirname(args.manifest) or ".", exist_ok=True)
    with open(args.manifest, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    print(f"\ncloned {len(cloned)}, skipped {len(present)}, failed {len(failed)} -> {args.manifest}")
    if failed:
        print(f"retry with: python clone_mirror.py --retry-failed {args.manifest}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
