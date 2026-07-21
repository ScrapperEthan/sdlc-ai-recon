# RUNBOOK 45 (INTERNAL Codex / you) — verify UAT Use Case catalog (Round A) + collect owner answers

> **After Sonnet 5 builds `docs/specs/use-case-uat-catalog.md` (Round A) and pushes, pull `master`,
> then run this on the box with the REAL UAT CSVs.** Round A rebuilt Use Case ingest to be
> environment-aware and to read all three UAT tables; this is the first time it meets real UAT data.
> Two jobs: **Part A** confirms the numbers come out right (data), **Part B** collects the owner
> answers that steer Round B (rule_text AST, Catalog UI). Fill both and send back.
>
> **Data security:** the UAT CSVs, the Word doc, and any person-name-bearing derived file must **NOT**
> be committed or pushed. They live only under gitignored `index/usecase-snapshots/`. Do not paste raw
> person names into the send-back — counts and yes/no only.

---

# Part A — data verification

## Step A1 — place the UAT dataset (no commit)
Create the dataset dir and drop the three UAT CSVs + a manifest:
```
index/usecase-snapshots/uat/20260720-1730/
  manifest.json
  tbl_use_case.snapshot.csv                 (= tbl_use_case_202607201718.csv)
  tbl_use_case_channel_rule.snapshot.csv    (= tbl_use_case_channel_rule_202607201718.csv)
  tbl_use_case_ext.snapshot.csv             (= tbl_use_case_ext_202607201730.csv)
```
`manifest.json` = `{"environment":"UAT","snapshot_id":"20260720-1730","exported_at":"2026-07-20T17:30:00+08:00","tables":{"tbl_use_case":{"file":"tbl_use_case.snapshot.csv","row_count":2810},"tbl_use_case_channel_rule":{"file":"tbl_use_case_channel_rule.snapshot.csv","row_count":6217},"tbl_use_case_ext":{"file":"tbl_use_case_ext.snapshot.csv","row_count":2660}}}`

Note: **no** `tbl_event_router_usecase_topic` for UAT → the route dimension must report *unavailable*,
NOT fall back to the old dev/SCT snapshot. Point the loader at it and restart:
```
set SDLC_USECASE_DATASET=index/usecase-snapshots/uat/20260720-1730   (PowerShell: $env:SDLC_USECASE_DATASET=...)
```

## Step A2 — tests + quality report
```
python -m unittest discover -s . -p "test_*.py"
python refresh.py            (or the quality-report entry the spec wires in)
```
Expect **all green** (the count grows vs the prior run — new UAT header/contract tests were added).
Then open `index/reports/USECASE_QUALITY.md`.

## Step A3 — confirm the headline numbers (expected ← from the UAT analysis; a few IDs were photo-blurry, small drift is fine)

| Check | Expected | Actual |
|---|---|---|
| `tbl_use_case` rows | 2,810 | |
| `tbl_use_case_channel_rule` rows | 6,217 | |
| `tbl_use_case_ext` rows | 2,660 | |
| three-table join (all 3 have the UC) | 2,630 | |
| provenance `environment` | **UAT** (not dev/SCT) | |
| `column_bindings.status` | **`status`** (not `unknown_bounce_back_status`) | |
| status counts | Y = 2,697 · N = 113 | |
| missing `source_system` | ~3.1% (2,723 non-blank) | |
| illegal `business_category` codes | include **33 and 37** | |
| `marketing_insight_push_optin_flag` = Y detected | ≥ 3 (0 = still the plural bug) | |
| canonical `source_system` counts | MDC ~880 · PEGA ~479 · eAlert ~195 · PowerCard ~107 | |
| eAlert variants folded to one canonical | yes (e-Alert/ealert/… = 1 row) | |
| `MDC Test` NOT merged into `MDC` | separate | |

## Step A4 — the cross-environment guard (the P0 that mattered most)
Temporarily point the OLD dev/SCT route snapshot at this UAT dataset (or leave it discoverable) and ask
for `source-system:PEGA`. The report must **refuse the route join** — route dimension `unavailable
(no same-environment route snapshot)` — and must **NOT** print the old wrong shape
(`master_and_routing 297 / routing_only 56 / master_only 2513`). Confirm: `[ route dimension =
unavailable, not a silent count? ]`

## Step A5 — `source-system:PEGA` reads correctly on UAT
Ask the Q&A / CLI `impact_report.py source-system:PEGA`:

| Check | Expected | Actual |
|---|---|---|
| total / active | 479 / 415 | |
| `configured` (has channel rule) | ~479 (NOT ~87) | |
| endpoint repos surfaced (e.g. `mc-hk-hase-pega-adapter-job`) | yes, with confidence | |
| disabled (status=N) excluded by default; `include_inactive` adds them | yes | |
| MDC (~880 UCs) response is paginated, not a full dump | yes | |

---

# Part B — owner confirmation (answers steer Round B; not blockers for A)

**Data / business owner:**
1. `business_category` **33** and **37** — official names + which router/topic they map to?
2. `source_system` — is there a controlled vocabulary / CMDB ID, or is it free text? (decides how far
   canonicalization goes vs the alias registry)
3. `rule_text` — official grammar + operator precedence? Specifically, is `LETTER > (EMAIL & SMS)`
   "LETTER first, then EMAIL+SMS in parallel on failure"?
4. endpoint `->` chains — real call chain, version-migration chain, or annotation?
5. `delivery_mode` — migrated numeric→string, or did the UAT export convert it?
6. `status=N` — is it guaranteed the UAT **runtime** never loads a disabled use case? (current code shows
   no status filter — see below)
7. Can we also export `tbl_use_case_router`, AEM template, department mapping, `ext_2way`? (unlocks
   Round C: real router→vendor→SLA, template lineage, dept-based notification, two-way graph)

**Runtime owner — raise as separate MDC issues; we do NOT change product runtime code here:**
8. `UseCaseService.findById` — no `status` filter (disabled UC may still load).
9. `MessageDirectorService` rule_text — no null/blank guard (99 active-missing-Ext + 6 blank rule_text
   may NPE / produce empty channels).
10. Null-priority comparator — no `nullsFirst/nullsLast` (4 null-priority UCs: M8765/M9992/M9994/M9996).
11. `PUSH+INBOX` — 7 active rules, DB/Java `PUSH_INBOX` enum exists but no Message Director. Placeholder
    config or missing runtime code?
12. Is the UAT-deployed code version the same as our current 390-repo CodeGraph index? (runtime-risk
    conclusions need re-check against the UAT tag/commit.)

---

# Send back
```
Part A
 A2  [ all tests green? new count = ___ ]
 A3  [ 2810/6217/2660? join 2630? env=UAT? status bound to `status`? Y2697/N113?
       illegal cats include 33 & 37? insight-consent ≥3? MDC~880/PEGA~479/eAlert~195 canonical? ]
 A4  [ route dimension = UNAVAILABLE (not 297/56/2513)? ]
 A5  [ PEGA total/active 479/415? configured ~479 not ~87? endpoint repos shown? disabled excluded?
       MDC paginated? ]
Part B
 1–7   [ business/data answers ]
 8–12  [ runtime owner ack / ticket ids ]
```

## Notes
- The route dimension being *unavailable* for UAT is **correct**, not a regression — coverage for UAT
  comes from the `configured/expression_ready/entrypoint_traceable/catalog_only` funnel, not the old
  dev/SCT route snapshot. If a same-environment route snapshot is ever exported, drop it into the
  dataset dir and the route dimension lights up automatically.
- Round A reports **channels from the `channel_rule.channel` column (fact)**. The initial/parallel/
  fallback decision tree is deliberately NOT computed yet — that is Round B's rule_text AST. Don't
  report "no fallback shown" as a bug.
- A small drift from the expected numbers is expected (some IDs in the source analysis were read off
  blurry photos). A *large* drift (e.g. status still 618/1666/526, or PEGA configured ~87) means a P0
  fix did not land — flag it.
