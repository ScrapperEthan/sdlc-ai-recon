# RUNBOOK 22 (INTERNAL Codex) — re-run: canonical 390 universe (drop the extract's non-system extras)

> **Who runs: INTERNAL Codex on the box.** **Pull `master` first.** Same one-command flow; this run
> fixes an over-inclusion from RUNBOOK-21. Read-only over the extract; writes only `recon_out/**` +
> `index/**`. **Don't push — relay `index/reports/REFRESH-SUMMARY.md` back.**

## What RUNBOOK-21 revealed, and what changed
RUNBOOK-21's summary showed **`repo tags: total 456`**, not ~390. Cause: the extract has **456 repo
directories** (`recon summary: repos scanned 456` — 395 with a Maven pom, 61 non-Maven), but our system
is **390** (`bundles.json` primary; the MDC sheet's "our" repos). The 66 difference are the extract's
**non-system extras** (support/infra/frontend dirs). RUNBOOK-21 seeded the tag universe from *all
scanned dirs*, so it pulled those 66 in — inflating `channel_true_dark` (209→254) and adding a few
non-system delivery-jobs (109→115), which would leak into outage/impact analysis.

**Fix (now on master):** the tag universe is seeded from the **canonical bundle plan** (`bundles.json`
`primary ∪ with_libs`) **∪ Maven edge endpoints** — i.e. exactly the 390-repo system, still covering the
5 edge-less repos RUNBOOK-21 recovered (they're in the plan), but **excluding** the 66 non-system dirs.
`bundles.json` stays frozen (read-only; `make_bundles` is NOT run).

## Step 0 — bundles check (unchanged)
```
python -c "import json;b=json.load(open('index/bundles.json',encoding='utf-8'));r={x for m in b.values() for x in m['primary']};print('bundles',len(b),'primary repos',len(r))"
```
Expect **31 bundles / ~390 primary repos**. If small/partial, STOP and report.

## Step 1 — one command (same as before)
```
python refresh.py --mirror "C:\Users\45509915\Downloads\HASE_MDC"
```
Ends with `steps failed: N`; writes `index/reports/REFRESH-SUMMARY.md` and `index/last_indexed.json`.

## What to expect this time
- **`repo tags: total` drops from 456 to ~390** (the canonical system; 5 recovered repos still present).
- **`channel_true_dark` drops back to ~200** (the 66 non-system extras no longer inflate it).
- **`delivery topology` delivery-jobs may drop a little** (non-system deli-job-named dirs removed) —
  that's the intended cleanup, not a regression.
- **`serves_channel_set` stays ~163** (Maven blast-radius ceiling — unchanged; message map is the
  separate next layer).
- **`arch map` stays ~13 bound / 29 empty** (external/vendor/topic nodes grey by design; HARO / CN-Gateway
  still need the hand override — separate task).

## Send back
Paste **`index/reports/REFRESH-SUMMARY.md`** plus:
```
Step 0 bundles:      [ #bundles / #primary ]
repo tags total:     [ should be ~390, down from 456 ]
refresh steps failed:[ N — stderr_tail from last_indexed.json if N>0 ]
```

## What this establishes
The retrieval surface now models **exactly our 390-repo system** — no non-system dirs leaking into tags,
topology, or outage analysis. Remaining known items (both already planned, neither blocks this): the
**message map** for the ~200 messaging-only repos, and the `arch_map.override.json` for HARO / CN-Gateway.
