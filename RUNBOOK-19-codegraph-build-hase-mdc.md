# RUNBOOK 19 (INTERNAL Codex) — build all-bundle CodeGraph from the local HASE_MDC extract

> **Who runs: INTERNAL Codex on the box**, in the **elevated CodeGraph shell** (SQLite writes need it),
> with `codegraph` on PATH. **Pull `master` first.** Runs `build_codegraph.py` (already on master) against
> the local full-source extract — **no network clone needed** (RUNBOOK-18 is unnecessary). Read-only over
> the extract (copies out, never writes in); writes only `index/codegraph/**` + the manifest. Don't push —
> relay the manifest summary.

Source path (the extract confirmed to cover **all 31 bundles 100%** on 2026-07-13):
```
C:\Users\45509915\Downloads\HASE_MDC
```
If that path/username differs on the box, substitute it everywhere below.

## Guardrails (check before the long run)
- **Elevated shell + `codegraph` on PATH** (else the build errors out cleanly — fix and retry).
- **Free disk**: staging copies + per-bundle DBs across 31 bundles can total **tens of GB**. Confirm space.
- **Time**: minutes per bundle; the full run is **hours** (big bundles like `svc-rt` 48, `tracking` 59).
  Fine to leave running.

## Step 1 — dry run (re-confirm coverage, builds nothing)
```
python build_codegraph.py --mirror "C:\Users\45509915\Downloads\HASE_MDC" --dry-run
```
Expect every bundle `N/N present`.

## Step 2 — validate TWO bundles first (elevated)
Prove the recipe on this extract before the long run:
```
python build_codegraph.py --mirror "C:\Users\45509915\Downloads\HASE_MDC" --only platform-core
python build_codegraph.py --mirror "C:\Users\45509915\Downloads\HASE_MDC" --only tracking
```
Both should end `[done] ... ok, <seconds>s, <MiB> MiB`. If either fails, stop and report the error
(do not start the full run).

## Step 3 — full build (long; leave it running)
```
python build_codegraph.py --mirror "C:\Users\45509915\Downloads\HASE_MDC"
```
Writes `index/codegraph_build.json`. Re-runnable (a stale bundle staging dir is rebuilt).

## Step 4 — verify routing (the RUNBOOK-16 CLI gap is fixed)
```
python cli.py unified-impact IngressService --bundle misc-ingress-to-lys
python cli.py unified-impact mc-hk-hase-api-ingress-core     # seed is a repo -> routes by its bundle
```
Confirm `callers.available: true`, `returncode: 0`, real callers, and the `bundle_root` it used.

## Step 5 (optional) — Q&A app sees it
`python -m webapp.server` (:8765) → ask a "who calls / trace" question scoped to a built bundle → the
answer should use the real call graph (cross-repo callers), not lexical hits.

## Send back (paste this filled in)
```
Step 1 dry-run:  [ all bundles N/N? any not 100%? ]
Step 2 validate: [ platform-core + tracking: ok? seconds + MiB each ]
Step 3 full:     [ how many bundles built / failed; total time; index/codegraph_build.json summary; free disk after ]
Step 4 routing:  [ unified-impact --bundle + repo-seed: callers.available / returncode / bundle_root ]
Step 5 Q&A:      [ (optional) deep question answered from the real call graph? ]
Surprises:       [ ... ]
```

## What this establishes
Green = **all 31 domain CodeGraph indexes built from the local extract**, and retrieval **routes** to the
right one — so the Q&A app answers symbol-level "who calls / trace across repos", cited. That's Step 1
moving from dependency/channel level to **function-call level** across the estate. Next: wire routing +
`make_arch_map` into `refresh.py` for incremental re-index, and refresh the extract when it goes stale.
