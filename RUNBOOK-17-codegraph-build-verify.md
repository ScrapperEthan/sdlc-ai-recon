# RUNBOOK 17 (INTERNAL Codex) — run the reproducible CodeGraph builder + verify routing

> **Who runs: INTERNAL Codex on the box** (real `mirror/`, `index/bundles.json`), in the **elevated
> local mode** CodeGraph needs. Runs the code EXTERNAL Codex built from
> `docs/specs/codegraph-build-and-routing.md` (**pull `master` first**). **Read-only over `mirror/`**
> (the builder copies out, never writes in); writes only `index/codegraph/**` + the manifest. Don't
> push — relay results. Background: RUNBOOK-16 results.

## Prereq — a full source tree (the gate for "all bundles")
Build needs every bundle's repos on disk. **Two ways to get there:**
- **A local full extract (fastest — no network clone).** If you have a complete extract (e.g. the
  `HASE_MDC` download), point `--mirror` at it. Confirmed 2026-07-13: `build_codegraph.py --mirror
  "<HASE_MDC path>" --dry-run` → **all 31 bundles 100% present**. When this works, **RUNBOOK-18 (network
  clone) is unnecessary.**
- Or clone into `mirror/` via RUNBOOK-18 (`clone_mirror.py`).

Pass the same `--mirror <path>` on every `build_codegraph.py` call below. (Freshness note: an extract is a
snapshot — fine for structural call graphs now; refresh later via clone/refresh.)

## Task A — dry run, validate two bundles, then build all (in the elevated CodeGraph shell)
Let `M` = your full source path (the `HASE_MDC` extract or `mirror/`). Dry run first:
```
python build_codegraph.py --mirror "M" --dry-run
```
**Validate the new source on two bundles before the long run** (elevated):
```
python build_codegraph.py --mirror "M" --only platform-core
python build_codegraph.py --mirror "M" --only tracking
```
If both build clean, do the **full run** (long — it stages+indexes every bundle; can take hours):
```
python build_codegraph.py --mirror "M"
```
**Relay the manifest summary** (`index/codegraph_build.json`): per bundle — staged_count, returncode,
seconds, db_mib; and any failures. Sanity: numbers should be in the RUNBOOK-16 ballpark (ingress-ish
~125 MiB / ~380 s, tracking ~137 MiB / ~465 s). Before the full run, **check free disk** — staging copies
+ per-bundle DBs across 31 bundles can total tens of GB.

## Task B — routing via the CLI (the gap RUNBOOK-16 hit is fixed)
```
python cli.py unified-impact IngressService --bundle misc-ingress-to-lys
python cli.py unified-impact mc-hk-hase-api-ingress-core        # seed is a repo -> routes by its bundle, no --bundle needed
```
Confirm `callers.available: true`, `returncode: 0`, `fallback_hits: []`, real symbols/callers, and that
the payload shows the `bundle_root` it used. A seed with no built bundle → clean fallback (lexical), no crash.

## Task C — the Q&A app sees it (deep code question)
```
python -m webapp.server        # :8765
```
Ask a "who calls / trace" question scoped to a built bundle (e.g. *"who calls IngressService in the
ingress flow"*). Confirm the answer uses the **real call graph** (cross-repo callers, cited) rather than
lexical hits. (If the agent needs a bundle hint to route, note that — it's the best-effort limit in the spec.)

## Send back (paste this filled in)
```
Task A build:   [ dry-run plan; manifest per-bundle (staged/rc/seconds/MiB); platform-core builds now? skipped bundles ]
Task B routing: [ unified-impact with --bundle and repo-seed: callers.available/returncode/bundle_root; fallback clean? ]
Task C Q&A:     [ deep question answered from the real call graph? cross-repo callers shown? ]
Surprises:      [ ... ]
```

## What this establishes + the one remaining gate
Green = the per-bundle CodeGraph build is **one re-runnable command** and retrieval **routes to the right
index**, so the Q&A app answers symbol-level "who calls / trace" across a bundle — the deep-code layer on
top of the already-full dependency/message graphs. **The only thing between here and all 31 bundles is
cloning the full mirror** on the approved machine; `build_codegraph.py` then indexes them all unchanged.
Wire routing into the refresh chain next so it re-indexes changed bundles automatically.
