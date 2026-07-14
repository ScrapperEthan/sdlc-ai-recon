#!/usr/bin/env python3
"""
recon_maven_graph.py
Characterize a multi-repo Java estate and extract the cross-repo Maven
dependency graph.

Usage:
    python recon_maven_graph.py <ROOT_DIR> [OUT_DIR]

ROOT_DIR : a folder containing one sub-folder per cloned repo.
OUT_DIR  : where to write results (default: ./recon_out)

Pure stdlib. Reads only pom.xml / build.gradle presence. Nothing leaves the box.
"""

import os
import sys
import csv
import collections
import xml.etree.ElementTree as ET


def strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]
    return root


def parse_pom(path):
    try:
        root = strip_ns(ET.parse(path).getroot())
    except Exception:
        return None

    def t(parent, tag):
        e = parent.find(tag) if parent is not None else None
        return e.text.strip() if (e is not None and e.text) else None

    parent = root.find('parent')
    p_g, p_a, p_v = t(parent, 'groupId'), t(parent, 'artifactId'), t(parent, 'version')
    g = t(root, 'groupId') or p_g
    a = t(root, 'artifactId')
    v = t(root, 'version') or p_v
    packaging = t(root, 'packaging') or 'jar'
    modules = [m.text.strip() for m in root.findall('./modules/module') if m.text]
    deps = []
    for d in root.findall('./dependencies/dependency'):
        dg, da = t(d, 'groupId'), t(d, 'artifactId')
        if dg and dg.startswith('${project.groupId}'):
            dg = g
        if dg and da:
            deps.append((dg, da))
    return {
        'groupId': g, 'artifactId': a, 'version': v, 'packaging': packaging,
        'parent': (p_g, p_a) if (p_g and p_a) else None,
        'modules': modules, 'deps': deps,
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    root_dir = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else 'recon_out'
    os.makedirs(out_dir, exist_ok=True)

    repos = sorted(d for d in os.listdir(root_dir)
                   if os.path.isdir(os.path.join(root_dir, d)) and not d.startswith('.'))

    repo_poms = collections.defaultdict(list)
    repo_has_gradle = {}
    for repo in repos:
        base = os.path.join(root_dir, repo)
        gradle = False
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ('.git', 'target', 'build', 'node_modules')]
            for fn in filenames:
                if fn == 'pom.xml':
                    info = parse_pom(os.path.join(dirpath, fn))
                    if info:
                        repo_poms[repo].append(info)
                elif fn in ('build.gradle', 'build.gradle.kts'):
                    gradle = True
        repo_has_gradle[repo] = gradle

    produced = collections.defaultdict(set)        # (g,a) -> {repos that publish it}
    parent_refs = collections.Counter()            # (g,a) parent -> count
    for repo, poms in repo_poms.items():
        for p in poms:
            if p['groupId'] and p['artifactId']:
                produced[(p['groupId'], p['artifactId'])].add(repo)
            if p['parent']:
                parent_refs[p['parent']] += 1

    edges = []                                     # (from_repo, to_repo, via)
    consumers = collections.defaultdict(set)       # (g,a) -> {repos depending on it}
    for repo, poms in repo_poms.items():
        for p in poms:
            for ga in p['deps']:
                if ga in produced and repo not in produced[ga]:
                    for owner in produced[ga]:
                        if owner != repo:
                            edges.append((repo, owner, f"{ga[0]}:{ga[1]}"))
                    consumers[ga].add(repo)
            par = p['parent']
            if par and par in produced and repo not in produced[par]:
                for owner in produced[par]:
                    if owner != repo:
                        edges.append((repo, owner, f"{par[0]}:{par[1]} (parent)"))
                consumers[par].add(repo)

    edges = sorted(set(edges))

    n_total = len(repos)
    n_maven = sum(1 for r in repos if repo_poms.get(r))
    n_gradle_only = sum(1 for r in repos if repo_has_gradle.get(r) and not repo_poms.get(r))
    n_neither = sum(1 for r in repos if not repo_poms.get(r) and not repo_has_gradle.get(r))
    n_multimod = sum(1 for r in repos if any(p['modules'] for p in repo_poms.get(r, [])))
    indeg = collections.Counter(o for _, o, _ in edges)
    groupids = collections.Counter(g for (g, a) in produced)
    top_shared = sorted(((f"{g}:{a}", len(rs)) for (g, a), rs in consumers.items()),
                        key=lambda x: -x[1])

    with open(os.path.join(out_dir, 'internal_edges.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['from_repo', 'to_repo', 'via_artifact'])
        w.writerows(edges)
    # Full scanned repo list — the authoritative universe. Repos with no internal Maven edge
    # never appear in internal_edges.csv, so downstream tagging seeds from this to cover them.
    with open(os.path.join(out_dir, 'repos.txt'), 'w', encoding='utf-8') as f:
        for repo in repos:
            f.write(repo + "\n")
    with open(os.path.join(out_dir, 'produced.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['artifact', 'repos'])
        for (g, a), rs in sorted(produced.items()):
            w.writerow([f"{g}:{a}", ';'.join(sorted(rs))])
    with open(os.path.join(out_dir, 'top_shared.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['internal_artifact', 'num_dependent_repos'])
        w.writerows(top_shared)

    shared_lib_hits = sum(1 for _, c in top_shared if c >= 3)
    common_parent = parent_refs.most_common(1)
    touched = len(set(f for f, _, _ in edges) | set(o for _, o, _ in edges))

    out = []
    P = out.append
    P("=" * 60)
    P("RECON SUMMARY — multi-repo Java estate")
    P("=" * 60)
    P(f"repos scanned (sub-dirs)     : {n_total}")
    P(f"  with Maven pom.xml         : {n_maven}")
    P(f"  Gradle-only                : {n_gradle_only}")
    P(f"  neither (frontend/infra/..) : {n_neither}")
    P(f"  multi-module Maven repos   : {n_multimod}")
    P("")
    P(f"distinct internal artifacts  : {len(produced)}")
    P(f"distinct groupIds            : {len(groupids)}")
    P(f"  top groupIds               : {', '.join(g for g, _ in groupids.most_common(5))}")
    P("")
    P(f"internal dependency edges    : {len(edges)}")
    P(f"repos touched by the graph   : {touched}")
    if common_parent:
        (pg, pa), pc = common_parent[0]
        P(f"most-used parent POM         : {pg}:{pa}  (declared by {pc} poms)")
    P("")
    P("VERDICT")
    P(f"  Maven multi-repo + shared libs : {'YES' if shared_lib_hits else 'NO / UNCLEAR — inspect edges'}")
    P(f"  (shared lib = internal artifact used by >=3 repos; found {shared_lib_hits})")
    P("")
    P("TOP 15 SHARED INTERNAL LIBRARIES (where a change ripples from)")
    for art, c in top_shared[:15]:
        P(f"  {c:>4}  {art}")
    P("")
    P("TOP 10 HUB REPOS BY DEPENDENTS")
    for repo, c in indeg.most_common(10):
        P(f"  {c:>4}  {repo}")
    P("")
    P(f"full repo list written        : {n_total} repos -> repos.txt")
    P(f"outputs in: {out_dir}/  (internal_edges.csv, repos.txt, produced.csv, top_shared.csv)")
    summary = "\n".join(out)
    print(summary)
    with open(os.path.join(out_dir, 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write(summary + "\n")


if __name__ == '__main__':
    main()
