#!/usr/bin/env python3
"""
harvest_poms.py
Download ONLY pom.xml files from every repo in a GitHub org/owner, without
cloning, using the authenticated `gh` CLI. Cross-platform (Windows-friendly:
no bash, no base64 binary needed).

Output tree:  <OUT>/<repo>/<path>/pom.xml   -> feeds recon_maven_graph.py

Usage:
    python harvest_poms.py <ORG_OR_OWNER> [OUT_DIR]

Requires: gh (authenticated). For an internal/enterprise GitHub, first run
`gh auth login --hostname <host>` and set GH_HOST (PowerShell:
`$env:GH_HOST="<host>"`). Python 3.7+. Nothing leaves the machine.
"""
import os
import sys
import base64
import subprocess


def gh(args):
    """Run a gh command; return (ok, stdout)."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.returncode == 0, r.stdout
    except FileNotFoundError:
        print("ERROR: `gh` not found on PATH. Install GitHub CLI and `gh auth login`.")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    org = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "poms"
    os.makedirs(out, exist_ok=True)

    ok, data = gh(["repo", "list", org, "--no-archived", "--limit", "1000",
                   "--json", "name", "-q", ".[].name"])
    if not ok:
        print(f"ERROR: could not list repos for '{org}'. "
              f"Check access and GH_HOST (internal GitHub host).")
        sys.exit(1)
    repos = [r.strip() for r in data.splitlines() if r.strip()]
    total = len(repos)
    print(f"Found {total} repos under '{org}'. Harvesting pom.xml only ...")

    for i, repo in enumerate(repos, 1):
        ok, br = gh(["api", f"repos/{org}/{repo}", "--jq", ".default_branch"])
        branch = br.strip()
        if not ok or not branch:
            print(f"[{i}/{total}] {repo}: skip (no access)")
            continue

        ok, tree = gh(["api", f"repos/{org}/{repo}/git/trees/{branch}?recursive=1",
                       "--jq", '.tree[] | select(.path | endswith("pom.xml")) | .path'])
        paths = [p.strip() for p in tree.splitlines() if p.strip()] if ok else []
        if not paths:
            print(f"[{i}/{total}] {repo}: no pom.xml (non-Maven?)")
            continue

        n = 0
        for p in paths:
            ok, content = gh(["api", f"repos/{org}/{repo}/contents/{p}?ref={branch}",
                              "--jq", ".content"])
            if not ok or not content.strip():
                continue
            try:
                raw = base64.b64decode(content)
            except Exception:
                continue
            dest = os.path.join(out, repo, *p.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(raw)
            n += 1
        print(f"[{i}/{total}] {repo}: {n} pom(s)")

    print(f"\nDone -> {out}/")
    print(f"Next:  python recon_maven_graph.py {out}")


if __name__ == "__main__":
    main()
