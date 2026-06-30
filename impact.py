#!/usr/bin/env python3
"""
impact.py — query the cross-repo dependency graph (blast radius).

Reads recon_out/internal_edges.csv (columns: from_repo,to_repo,via_artifact;
meaning "from depends on to").

Usage:
    python impact.py <repo_name>              # direct dependents + dependencies
    python impact.py <repo_name> --transitive # full ripple, all hops
    python impact.py --hubs                   # most depended-on repos
    python impact.py <repo_name> --edges PATH # custom CSV path
"""
import csv
import sys
import collections


def load(path):
    fwd = collections.defaultdict(set)   # repo -> repos it depends on
    rev = collections.defaultdict(set)   # repo -> repos that depend on it
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            a, b = row['from_repo'].strip(), row['to_repo'].strip()
            if a and b:
                fwd[a].add(b)
                rev[b].add(a)
    return fwd, rev


def bfs(graph, start):
    seen, stack = set(), [start]
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    seen.discard(start)
    return seen


def main():
    argv = sys.argv[1:]
    path = "recon_out/internal_edges.csv"
    if "--edges" in argv:
        i = argv.index("--edges")
        path = argv[i + 1]
        del argv[i:i + 2]
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]

    try:
        fwd, rev = load(path)
    except FileNotFoundError:
        print(f"ERROR: {path} not found. Run recon_maven_graph.py first.")
        sys.exit(1)

    if "--hubs" in flags:
        print("Most depended-on repos (bigger = change ripples wider):")
        for k, v in sorted(rev.items(), key=lambda kv: -len(kv[1]))[:20]:
            print(f"  {len(v):>4}  {k}")
        return

    if not args:
        print(__doc__)
        return

    x = args[0]
    if "--transitive" in flags:
        up, down, mode = bfs(rev, x), bfs(fwd, x), "transitive (all hops)"
    else:
        up, down, mode = rev.get(x, set()), fwd.get(x, set()), "direct (1 hop)"

    print(f"[{x}]  {mode}")
    print(f"\n  UP — depended on by ({len(up)})  | changing {x} can ripple to these:")
    for r in sorted(up):
        print(f"      {r}")
    print(f"\n  DOWN — {x} depends on ({len(down)}):")
    for r in sorted(down):
        print(f"      {r}")


if __name__ == "__main__":
    main()
