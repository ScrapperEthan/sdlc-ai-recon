# RUNBOOK 16 (INTERNAL Codex) — build per-bundle CodeGraph indexes (leadership-approved)

> **Who runs: INTERNAL Codex on the box** (real `mirror/`, `index/bundles.json`). Leadership approved
> the CodeGraph builds. **Read-only over the estate** (CodeGraph reads code, writes only its own index
> dirs — never modify `mirror/`). Don't push — relay results (photo). Background: `docs/DOMAIN-PARTITION-PLAN-zh.md`
> (31 bundles, ~10.5 MiB/repo, ≤~60 repos/bundle → ≤~0.6 GiB each; build order in §6). This runbook
> **confirms the build interface + mirror coverage, then builds the first bundles** so we see it end-to-end;
> the reproducible all-31 loop + retrieval routing come next (I'll write them from your report).

## Prereq A — mirror coverage (what can we actually build?)
`index/bundles.json` lists 31 bundles, each with its repo set. The mirror must contain a repo's checkout
to index it. Report coverage:
```
python -c "import json,os;from retriever import config;b=json.load(open(config.BUNDLES_JSON,encoding='utf-8-sig'));m=config.MIRROR;rows=[]
for name,meta in b.items():
  repos=meta.get('primary',[]) if isinstance(meta,dict) else meta
  present=[r for r in repos if os.path.isdir(os.path.join(m,r))]
  rows.append((name,len(present),len(repos)))
rows.sort(key=lambda x:-x[1])
print('bundle                         present/total')
[print(f'{n:30} {p}/{t}') for n,p,t in rows]
print('mirror repo dirs total:', sum(1 for _ in os.scandir(m) if _.is_dir()))"
```
**Relay this table.** It tells us which bundles are fully clonable now vs still need repos in the mirror.
(If the mirror is still the 15-repo pilot, we build the pilot bundles first and clone the rest as we go.)

## Prereq B — the CodeGraph build interface (MOST IMPORTANT — I need this to script the rest)
```
codegraph --help
codegraph init --help        # or: codegraph build --help
```
Report **exactly**: the command that **creates** an index over a set of repos, **where the index is
written** (a `.codegraph/` dir in the cwd? a `--db`/`--out` path?), whether it takes **one repo**, a
**directory of repos**, or a **list**, and **how the 15-repo pilot index was originally built** (the command
+ where its `.codegraph` lives). This is the one fact I can't derive off-box; with it I write the
reproducible `build_codegraph.py`.

## Task A — build the first bundles (plan order §6: ingress + tracking, then platform-core)
Using the confirmed command, build each bundle into its **own index location** (keep them separate — do
NOT build one 390-repo graph). Suggested layout: `index/codegraph/<bundle>/` (gitignored). For a bundle
whose repos live under `mirror/<repo>/`, point CodeGraph at that bundle's repo set. Record **wall-clock
seconds + on-disk MiB** per bundle. Start with:
1. `ingress` and `tracking` (the original pilot flow — validates the bundle build end-to-end).
2. `platform-core` (every domain references it).
Stop after these 2–3 and report, before grinding the long tail.

## Task B — verify a real symbol resolves (not the lexical fallback)
Pick a real cross-repo call inside a built bundle (e.g. an ingress→core call). From that bundle's index:
```
codegraph explore "<a real method/class in that bundle>"        # real call paths, not just grep
```
Then confirm the retrieval layer sees it. Note: `retriever/unified_impact.py` runs `codegraph explore`
in the **current working dir**, so run from the bundle's index dir (or set its env), then:
```
python cli.py unified-impact "<same symbol>"      # "callers.available": true, real output (not the "codegraph CLI not on PATH / lexical" fallback)
```

## Send back (paste this filled in)
```
Prereq A coverage:  [ the bundle present/total table; mirror repo-dir count ]
Prereq B interface: [ the create-index command; where the index is written; repo/dir/list?; how the pilot was built ]
Task A builds:      [ ingress / tracking / platform-core: seconds + MiB each; any repo that failed to index ]
Task B verify:      [ codegraph explore returns real call paths? cli.py unified-impact callers.available = true? ]
Surprises/errors:   [ ... ]
```

## What this unlocks + where you'll see it
- **Impact:** today `codegraph explore` / `call_graph` only works where a single index sits in the cwd
  (the pilot); everywhere else the retriever **falls back to lexical grep**. Per-bundle indexes light up
  **symbol-level call paths** ("who calls this method", "trace this flow across repos") **within each
  bundle**. Cross-bundle **dependency + message** impact already works today (full CSV graphs, unchanged).
- **Where to interact:** the **Q&A app (`python -m webapp.server`, :8765)** — deep "who calls / trace"
  questions get real call-graph answers; also `codegraph explore` directly and `cli.py unified-impact`.
- **Honest gap (the follow-up I'll build from your report):** the retriever does **not yet route** a
  query to the right bundle's index — it shells `codegraph explore` in the cwd. So until that routing +
  the reproducible `build_codegraph.py` land, you interact per-bundle (run from that bundle's index dir).
  Your Prereq-B answer is exactly what lets me write both.
