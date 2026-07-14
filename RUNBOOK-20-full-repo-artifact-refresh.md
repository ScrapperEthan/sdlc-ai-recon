# RUNBOOK 20 (INTERNAL Codex) — refresh all full-repo retrieval artifacts from the HASE_MDC extract

> **Who runs: INTERNAL Codex on the box.** **Pull `master` first** (this runbook needs the updated
> `refresh.py` that now runs the full chain). Read-only over the extract (reads `pom.xml` presence only,
> nothing leaves the box); writes only `recon_out/**` and `index/**` generated artifacts.
> **Don't push — relay `index/reports/REFRESH-SUMMARY.md` back** (one small file).
>
> **Runs in PARALLEL with (or right after) the RUNBOOK-19 CodeGraph build.** This chain is stdlib
> graph-parsing — **minutes**, not the CodeGraph build's hours. `refresh.py` now re-points the retrieval
> artifacts (graph → repo tags → delivery topology → arch-map, plus the MDC business-metadata overlay)
> at the **same** extract the CodeGraph build uses, so arch.html / impact / outage stop being stuck on
> the old ~16-repo `mirror/` and go full (~390). One command does all of it.

Source path (same extract as RUNBOOK-19, confirmed to cover all 31 bundles 100% on 2026-07-13):
```
C:\Users\45509915\Downloads\HASE_MDC
```
If that path/username differs on the box, substitute it in the commands below.

## ✅ Bundle safety is automatic
`refresh.py` **does not run `make_bundles.py`** — it reads the existing frozen `index/bundles.json` and
only rebuilds everything downstream of it. So `repo_tags.json`'s `bundle` field stays consistent with the
per-bundle CodeGraph indexes the routing points at. **Do not run `make_bundles.py` by hand** during this.

## Guardrails (check before running)
- **`MDC_Repo_List_Analysis.xlsx`** at the repo root (or set `SDLC_MDC_SHEET`), tab `full Repository List`,
  headers **verbatim including the typos** (`WhatsAPP`, `Maraketing/Servicing(M/S)`, `TimeCritcal(Y/N)`).
  If it's missing/mis-headed the MDC step is skipped (or errors) but the rest still completes — the
  summary just won't have the reconcile line.
- Idempotent: safe to re-run; it overwrites the same `index/` files.

## Step 0 — confirm bundles.json is the full, frozen one (don't proceed if not)
```
python -c "import json;b=json.load(open('index/bundles.json',encoding='utf-8'));r={x for m in b.values() for x in m['primary']};print('bundles',len(b),'primary repos',len(r))"
```
Expect **31 bundles** and **~390 primary repos** (the same file RUNBOOK-19's dry-run showed 100%).
If it's small/partial, **STOP and report** — bundles are out of sync with the CodeGraph build.

## Step 1 — one command: refresh everything from the extract
```
python refresh.py --mirror "C:\Users\45509915\Downloads\HASE_MDC"
```
This runs, in order: `recon_maven_graph` → `make_repomap` → `enrich_repo_tags` (MDC overlay) →
`make_repo_tags` (+ `serves_channels`) → `make_delivery_topology` → MDC reconcile report →
`message_map_enrich` (if `index/message_edges.csv` present) → `make_arch_map`. It prints
`steps failed: N` at the end and writes:
- `index/reports/REFRESH-SUMMARY.md` ← **the file to send back**
- `index/last_indexed.json` (full per-step log, if we need to dig into a failure)

If `steps failed:` is not `0`, open `index/last_indexed.json`, find the step with a non-zero
`returncode`, and paste its `stderr_tail` in your report.

## Step 2 — smoke-test the UI (optional but nice)
```
python -m webapp.server
```
Open `impact.html` and `arch.html`; click **SMS / Sinch / WhatsApp** nodes and confirm the bound repos
and `查故障影响` outage sets now cover the full estate (WhatsApp→HARO, WeChat→CN-Gateway still correct).

## Send back
Just paste the contents of **`index/reports/REFRESH-SUMMARY.md`** (it's a few lines), plus:
```
Step 0 bundles:   [ #bundles / #primary repos ]
refresh steps failed: [ N — and the failing step's stderr_tail if N>0 ]
Step 2 UI:        [ (optional) SMS/Sinch/WhatsApp show full repos + outage set? screenshots ]
Surprises:        [ ... ]
```
Expected in the summary: `maven graph` jumps to ~390 repos; `repo tags` shows `serves_channel_set`
approaching ~390 with a small `channel_true_dark`; `arch map` shows most nodes bound (few empty).

## What this establishes
Green = the **dependency/channel-level** retrieval surface (arch.html, impact, outage, `serves_channels`,
MDC business metadata) now reflects the **full ~390-repo estate**, matching the **function-call-level**
CodeGraph build from RUNBOOK-19. Both halves of Step 1 are then full at the same source of truth, from a
single re-runnable `python refresh.py`.
