# RUNBOOK 24 (INTERNAL Codex) — get the CodeGraph function-call layer fully live

> **Who runs: INTERNAL Codex on the box**, in the **elevated CodeGraph shell** (SQLite writes).
> **Pull `master` first** — it fixes a manifest bug and adds `--reconcile`. Read-only over the extract;
> writes only `index/codegraph/**` + the manifest. **Don't push — relay the two numbers at the end.**

## Why (what RUNBOOK-23 found)
RUNBOOK-23 showed the manifest with **only 1 bundle (`tracking`)**, so `unified-impact` routes only for
`tracking` and FAILS for every other bundle. Root cause: `build_codegraph.py` **overwrote** the whole
manifest on an `--only <bundle>` run, wiping the other bundles' records (routing reads that manifest).
Fixed now: the manifest **merges/upserts**, and a new **`--reconcile`** recovers manifest entries from
already-built index dirs without rebuilding.

Two possibilities — the diagnostic below tells us which:
- **A.** The full 31-bundle build actually completed earlier, but a later `--only` run clobbered the
  manifest → the index dirs are on disk; we just need `--reconcile` (**seconds**).
- **B.** The full build never finished (only `tracking` was ever built) → we must run it (**hours**).

## Step 1 — diagnose: how many bundle indexes are actually on disk?
```
python -c "import os;r='index/codegraph';d=[x for x in os.listdir(r) if os.path.isfile(os.path.join(r,x,'.codegraph','codegraph.db'))] if os.path.isdir(r) else [];print('built index dirs:',len(d));print(sorted(d))"
```
- **Many (~31)** → go to **Step 2A**.
- **Just `tracking`** (or 1–2) → go to **Step 2B**.

## Step 2A — completed build, clobbered manifest → reconcile (seconds)
```
python build_codegraph.py --reconcile
```
Writes every on-disk index back into `index/codegraph_build.json`. Then go to **Step 3**.

## Step 2B — full build never finished → run it (hours; leave it running)
Elevated shell, `codegraph` on PATH:
```
python build_codegraph.py --mirror "C:\Users\45509915\Downloads\HASE_MDC"
```
Re-runnable; the merge fix means the manifest ends up complete (all 31). Big bundles (`svc-rt` 48,
`tracking` 59) take minutes each; the whole run is hours. Then go to **Step 3**.

## Step 3 — re-verify routing (the RUNBOOK-23 A2 gap should be gone)
```
python -c "import json;m=json.load(open('index/codegraph_build.json',encoding='utf-8'));b=m['bundles'];print('bundles ok:',sum(1 for x in b if x.get('returncode')==0),'/',len(b))"
python cli.py unified-impact IngressService --bundle misc-ingress-to-lys
```
Now `unified-impact` for a **non-tracking** bundle should return `callers.available: true`,
`returncode: 0`, and a real `bundle_root` (not the repo-root fallback).

## Send back
```
Step 1 on-disk index dirs: [ count + which ]
Path taken:                [ 2A reconcile / 2B full build ]
Step 3 manifest:           [ bundles ok / total ]
Step 3 routing:            [ unified-impact misc-ingress-to-lys: callers.available / bundle_root ]
```

## What this establishes
Green = the **function-call layer is live for all bundles**, not just `tracking` — the Q&A app can answer
"who calls / trace across repos" routed to the right index across the estate. That closes the RUNBOOK-23
gap. (Separate, already-planned: regenerate `bundles.json` from the full graph once the build settles;
the message map for the ~214 messaging-only repos.)
