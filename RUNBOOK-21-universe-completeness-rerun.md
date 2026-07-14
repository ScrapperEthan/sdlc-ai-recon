# RUNBOOK 21 (INTERNAL Codex) — re-run the refresh after the universe-completeness fix

> **Who runs: INTERNAL Codex on the box.** **Pull `master` first** — it now contains the
> universe-completeness fix. Same one-command flow as RUNBOOK-20; this just closes the `387 vs 390`
> gap. Read-only over the extract; writes only `recon_out/**` + `index/**`. **Don't push — relay
> `index/reports/REFRESH-SUMMARY.md` and `recon_out/summary.txt` back.**

## What changed (why re-run)
Previously `make_repo_tags` built its repo universe from **Maven edge endpoints only**, so repos with
no internal Maven dependency (config / Gradle / Python / isolated libs) dropped out → the run showed
**387** repos vs the **390** in `bundles.json`; 5 named missing (`mc-hk-hase-aws-pipeline-config`,
`mc-hk-hase-commonbus-sdk`, `shp-pipeline-configuration`, `shp-pipeline-shared-lib`,
`shp-pipeline-shared-lib-python`). Now:
- `recon_maven_graph.py` emits **`recon_out/repos.txt`** — the full scanned repo list.
- `make_repo_tags.py` **seeds the universe from `repos.txt`** (via `refresh.py`), so every scanned repo
  gets a tag entry (name-derived tags + MDC metadata + its `bundle` from the frozen `bundles.json`),
  with an honestly-empty `serves_channels` when nothing channel-owning depends on it.

## ✅ Bundle safety still automatic
`refresh.py` still **does not run `make_bundles.py`** — `bundles.json` stays frozen, consistent with the
CodeGraph indexes. **Do not run `make_bundles.py` by hand.**

## Step 0 — confirm bundles.json is the full, frozen one (unchanged)
```
python -c "import json;b=json.load(open('index/bundles.json',encoding='utf-8'));r={x for m in b.values() for x in m['primary']};print('bundles',len(b),'primary repos',len(r))"
```
Expect **31 bundles / ~390 primary repos**. If small/partial, STOP and report.

## Step 1 — one command (same as before)
```
python refresh.py --mirror "C:\Users\45509915\Downloads\HASE_MDC"
```
(Substitute the path if the box differs.) Ends with `steps failed: N`; writes
`index/reports/REFRESH-SUMMARY.md` and `index/last_indexed.json`.

## What to expect (corrected — read this so nothing looks "wrong")
- **`repo tags: total`** should rise from **387 toward ~390+** and the 5 named repos should now be
  present. This is the whole point of the re-run.
- **`serves_channel_set` stays ~163 and `channel_true_dark` stays large (~200).** This is EXPECTED and
  correct: `serves_channels` is **Maven blast-radius only** — it covers the shared-library spine. The
  other repos reach a channel via **messaging (Kafka)**, which is a separate layer (the message map),
  **not** something this fix changes. Do not expect it to jump.
- **`arch map`** binds only repo-backed nodes; external/vendor/topic nodes stay grey by design
  (HARO / CN-Gateway still need a hand override — separate small task).

## Send back
1. The contents of **`index/reports/REFRESH-SUMMARY.md`** (few lines).
2. The contents of **`recon_out/summary.txt`** — specifically these lines so we can confirm the graph
   isn't undercounting and see the non-Maven share:
   - `repos scanned`, `with Maven pom.xml`, `Gradle-only`, `neither (frontend/infra/..)`
   - `internal dependency edges`, `repos touched by the graph`, `most-used parent POM`
   - the **TOP 15 SHARED INTERNAL LIBRARIES** block (does `commonbus-sdk` appear with dependents?)
3. `refresh steps failed: N` (and the failing step's `stderr_tail` from `index/last_indexed.json` if N>0).

## What this establishes
Every repo in the extract now has a tag entry (closing `387 vs 390`), and `recon_out/summary.txt`
tells us how many repos are non-Maven and whether the Maven graph is complete — which decides whether
the ~163 `serves_channels` ceiling is purely "event-driven estate" (expected) or partly a parse gap to
chase. The real lever for the remaining repos' channel involvement is the **message map** (next
workstream), not this fix.
