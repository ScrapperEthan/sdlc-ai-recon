"""Dependency-graph queries over recon_out/internal_edges.csv."""
import csv
import collections
from . import config


def _load():
    fwd = collections.defaultdict(set)   # repo -> repos it depends on
    rev = collections.defaultdict(set)   # repo -> repos that depend on it
    try:
        with open(config.EDGES_CSV, newline='', encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                a = (r.get('from_repo') or '').strip()
                b = (r.get('to_repo') or '').strip()
                if a and b:
                    fwd[a].add(b)
                    rev[b].add(a)
    except FileNotFoundError:
        pass
    return fwd, rev


def _bfs(g, start):
    seen, stack = set(), [start]
    while stack:
        for n in g.get(stack.pop(), ()):
            if n not in seen:
                seen.add(n)
                stack.append(n)
    seen.discard(start)
    return seen


def impact(repo, transitive=False):
    """Who is affected if `repo` changes (depended_on_by) and what it needs (depends_on)."""
    fwd, rev = _load()
    if transitive:
        up, down = _bfs(rev, repo), _bfs(fwd, repo)
    else:
        up, down = rev.get(repo, set()), fwd.get(repo, set())
    return {
        "repo": repo,
        "mode": "transitive" if transitive else "direct",
        "depended_on_by": sorted(up),
        "depends_on": sorted(down),
    }


def hubs(top=20):
    """Most depended-on repos — the riskiest to change."""
    _, rev = _load()
    ranked = sorted(rev.items(), key=lambda kv: -len(kv[1]))[:top]
    return [{"repo": k, "dependents": len(v)} for k, v in ranked]


def known_repos():
    """All repos seen in the dependency graph."""
    fwd, rev = _load()
    return set(fwd) | set(rev)
