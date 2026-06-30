#!/usr/bin/env python3
"""
group.py — auto-derive a "business-flow bundle" of repos to index together with
CodeGraph, starting from ONE seed service. Uses the dependency graph (and the
message map, if present) so you do NOT need tribal knowledge to group repos by
flow — the grouping falls out of the graphs we already built.

Usage:
    python group.py <seed_repo> [--name-contains SUBSTR] [--max-hops N]
                    [--edges recon_out/internal_edges.csv]
                    [--messages index/message_edges.csv]

It prints the set of repos to clone into one ./mirror root, then `codegraph init .`

Logic: from the seed service, follow the dependency graph DOWNSTREAM (the shared
libs it needs) — this is bounded (~a dozen), it does NOT explode through hub libs
because we never follow "what depends on a hub". Then add message peers (repos
that share a queue/topic with anything already in the bundle). Optionally add
same-domain siblings by name.
"""
import csv
import sys
import collections


def take_opt(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        val = argv[i + 1]
        del argv[i:i + 2]
        return val
    return default


def load_edges(path):
    fwd = collections.defaultdict(set)
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            a, b = r['from_repo'].strip(), r['to_repo'].strip()
            if a and b:
                fwd[a].add(b)
    return fwd


def load_messages(path):
    dest_repos = collections.defaultdict(set)
    repo_dests = collections.defaultdict(set)
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            d = (r.get('destination') or '').strip()
            for col in ('producer_repo', 'consumer_repo'):
                v = (r.get(col) or '').strip()
                if d and v:
                    dest_repos[d].add(v)
                    repo_dests[v].add(d)
    return dest_repos, repo_dests


def bfs_down(fwd, seed, max_hops):
    seen, frontier, hop = {seed}, {seed}, 0
    while frontier and (max_hops is None or hop < max_hops):
        nxt = set()
        for n in frontier:
            for m in fwd.get(n, ()):
                if m not in seen:
                    seen.add(m)
                    nxt.add(m)
        frontier, hop = nxt, hop + 1
    return seen


def main():
    argv = sys.argv[1:]
    edges = take_opt(argv, '--edges', 'recon_out/internal_edges.csv')
    messages = take_opt(argv, '--messages', 'index/message_edges.csv')
    name_contains = take_opt(argv, '--name-contains')
    max_hops = take_opt(argv, '--max-hops')
    max_hops = int(max_hops) if max_hops else None
    args = [a for a in argv if not a.startswith('--')]
    if not args:
        print(__doc__)
        return
    seed = args[0]

    fwd = load_edges(edges)
    group = bfs_down(fwd, seed, max_hops)          # seed + libs it needs

    try:
        dest_repos, repo_dests = load_messages(messages)
        peers = set()
        for repo in list(group):
            for d in repo_dests.get(repo, ()):
                peers |= dest_repos[d]              # repos sharing a queue/topic
        group |= peers
    except FileNotFoundError:
        pass  # message map not built yet — dependency grouping still works

    if name_contains:
        all_repos = set(fwd) | {b for s in fwd.values() for b in s}
        group |= {r for r in all_repos if name_contains in r}

    print(f"# Flow bundle for seed: {seed}  ({len(group)} repos)")
    print("# clone these into one ./mirror root, then: codegraph init .")
    for r in sorted(group):
        print(r)


if __name__ == "__main__":
    main()
