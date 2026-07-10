# Spec (EXTERNAL Codex) — all-repo tags: MDC sheet business metadata + graph `serves_channels`

> **Who builds: EXTERNAL Codex.** Verify on the box via `RUNBOOK-12`. Goal: give **every** repo
> (all ~390) useful classification, from two sources — the box-local **MDC repo-list sheet**
> (business metadata) and the **full dependency graph** (channel blast-radius). **stdlib-only,
> read-only, writes only gitignored `index/` artifacts. Do NOT touch the Q&A app.**
>
> **Discovery is done (RUNBOOK-11) — build against these confirmed facts, do not re-guess the schema.**

## What the discovery settled (so we build the RIGHT thing)
- The sheet **does not fill the channel gap**: `unknown-by-name but sheet has a fixed channel = 0`.
  Every fixed-channel the sheet knows is already in the repo name. So the sheet is **NOT** a channel
  filler — it is **business metadata + a confirmation/mismatch source**. Do not let it overwrite the
  name-derived structural `channel`/`mode`/`system`.
- The sheet's genuinely-new fields (for our 390): **marketing/servicing** (103), **time-critical**
  (13), **business line CMB/WPB** (151), **MDC-Common / shared-component flag**. **No vendor column.**
- The real all-repo channel signal is **`serves_channels`** from the dependency graph (below).

## The MDC sheet — confirmed schema (box-local `MDC_Repo_List_Analysis.xlsx`, gitignored)
Workbook tabs: `full Repository List` (THE table), plus `split`, `Keyword in Repo Name` (ignore).
`full Repository List` header row (exact, **including the typos** — match them literally):

| Column (A→N) | Meaning | Values → normalised |
|---|---|---|
| `Repository` | repo-name key | exact match incl `mc-hk-…` prefix; still `lower()/strip()` for safe join |
| `MDC Common` | shared/common component flag | keep as `mdc_common` (truthy cell → true) |
| `SMS` `EMAIL` `PUSH` `WhatsAPP` `Letter` `Wechat` `Others` | channel columns, `Y` marks it | `SMS→sms, EMAIL→email, PUSH→push, WhatsAPP→whatsapp, Letter→letter, Wechat→wechat`; **`Others` is NOT a delivery channel** → bucket `other`/unclassified, never map to a real channel. **No `MMS` column.** |
| `Remark` | free-text description | **sensitive — do NOT write verbatim into any committed/relayed artifact**; keep only in gitignored `index/`, and prefer a length/emptiness flag over the text |
| `Batch/Realtime(B/R)` | mode | `R→realtime, B→batch, TBC→"" (unknown)` |
| `Maraketing/Servicing(M/S)` | marketing vs servicing (**typo `Maraketing`**) | `S→servicing, M→marketing` |
| `TimeCritcal(Y/N)` | time-critical flag (**typo `TimeCritcal`**) | `Y→true` else false/unknown |
| `CMB/WPB` | business line | `cmb`/`wpb` as present |

Shape facts: **one row per repo** (456 rows, 0 duplicates, **0 multi-channel rows** → channel is
effectively scalar; still emit list-shaped for compatibility). Our 390 repos are **all present**
(390/390); the sheet has ~66 extra repos not in our universe (ignore — key by repo name and let the
universe filter). **No multi-value-in-one-cell** anywhere. Value tallies (our 390): fixed-channel 129,
Others-only 21, mode 95, marketing/servicing 103, time-critical 13, business 151, **vendor 0**.

## Building block 1 — `enrich_repo_tags.py` → `index/repo_tags.mdc.json`
Stdlib `.xlsx` reader (the box has **no `openpyxl`**), read-only over `MDC_Repo_List_Analysis.xlsx`
(path via `--sheet`, default `config`-driven; add `MDC_SHEET_XLSX` to `retriever/config.py`). Parse
like RUNBOOK-11 confirmed:
- `zipfile.ZipFile(xlsx)` → read `xl/workbook.xml` + `xl/_rels/workbook.xml.rels` to resolve the
  **`full Repository List`** tab to its `xl/worksheets/sheetN.xml`; read `xl/sharedStrings.xml`.
- Cells with `t="s"` are **shared-string indices** (not literal text); also handle `t="inlineStr"`
  and numeric cells. **Blank cells are omitted from the XML** → map values by **column letter**
  (A=`Repository`, B=`MDC Common`, C=`SMS`, … N=`CMB/WPB`), never by positional packing.
- Emit `{ "<repo>": { <additive fields> } }` in a **separate namespace** so it can NEVER clobber the
  structural tags: `mdc_common` (bool), `marketing_servicing` (`marketing|servicing|""`),
  `time_critical` (bool), `business_line` (`cmb|wpb|""`), `channel_declared` (list, normalised; `Others`
  → `["other"]`), `mode_declared` (`realtime|batch|""`). Keep `Remark` **out** of this file (or as a
  boolean `has_remark`), per the sensitivity rule.
- Print a coverage table (repos seen, and per-field non-empty counts) so RUNBOOK-12 can photo it.

## Building block 2 — `serves_channels` in `make_repo_tags.py` (the real all-repo channel signal)
After tags are derived, for every repo compute:
`serves_channels(X) = sorted(⋃ channel(R) for R in impact(X) ∪ {X} if channel(R))`
where `impact(X)` = the **transitive dependents** of `X` (the same set `cli.py impact <repo>
--transitive` / `retriever.graph.impact()` already returns — reuse it, don't reimplement). Meaning:
if any channel-owning repo depends on `X`, an `X` outage hits that channel, so `X` **serves** it.
Store as a new list field `serves_channels` on each repo in `repo_tags.json`. This gives **every**
repo a channel involvement (empty only when nothing channel-tagged depends on it — a true leaf/infra).

Also fold in the MDC additive fields: load `index/repo_tags.mdc.json` (new `--mdc` arg, default from
`config`) and attach `mdc_common`/`marketing_servicing`/`time_critical`/`business_line` to each repo.
**Precedence, strict:** name-derived `channel`/`mode`/`system` stay authoritative; the MDC file only
**adds** its own-namespace fields; the existing hand-curated `--override` file still wins last (for
manual fixes). Do not overwrite structural fields from the sheet.

Extend `coverage_rows()` with: `serves_channel_set` (repos with a non-empty `serves_channels`),
`marketing_servicing_set`, `time_critical_set`, `mdc_common_set`, and a `channel_explained` count =
repos that are `channel_unknown` **but** carry `mdc_common`/`Others`/`serves_channels` (i.e. explained,
not a silent gap). The headline we want to see move: `channel_unknown` stays ~240, but
`serves_channel_set` should approach the full 390 and `channel_unknown AND serves_channels==[] AND not
mdc_common` (the true dark set) should be small.

## Building block 3 — reconciliation report `index/reports/TAG_RECONCILE.md` (+ JSON)
A small reporter (own script or a `--report` flag on `enrich_repo_tags.py`) that compares the sheet's
`channel_declared`/`mode_declared` against the name-derived values and lists:
- **Mismatches** (name says a channel, sheet says `Others`/blank — e.g. the `amet-mdc-…-sms-deli-job`
  cases) → for human review; **do not auto-resolve**, the structural name wins in the tags.
- **Confirmations** (name and sheet agree) — a count is enough.
- **Explained unknowns** — `channel_unknown` repos that are `mdc_common` or have `serves_channels`
  (so we can state honestly "X of 240 are shared/infra or covered by blast-radius, Y are truly dark").
Every row cites the sheet row and/or the graph edge it came from.

## Tests (stdlib fixtures, like existing)
- Tiny fixture `.xlsx` (build it in-test with `zipfile` + minimal XML, incl. a shared-strings index,
  a blank cell, and the header typos `WhatsAPP/Maraketing/TimeCritcal`) → parses to the right fields;
  `Others` → `other`, not a channel.
- `serves_channels`: a fixture graph where `lib` ← `sms-deli-job` (channel sms) → `serves_channels(lib)`
  includes `sms`; a repo nothing depends on → `[]`.
- Precedence: an MDC row with `Others` for a repo whose **name** is `…-sms-…` → tags keep `channel=[sms]`
  (name wins), and the reconcile report lists the mismatch.
- Coverage table includes the new rows; reconcile report renders with citations.

## Honesty / limits (put in the report + PLAN)
- The MDC sheet **does not raise channel coverage** (0 name-unknown repos gain a fixed channel from it);
  its value is **business metadata** (marketing/servicing, time-critical, business line, common-flag) +
  **confirmation/mismatch flags**. State this plainly so no one expects the 240 to vanish.
- All-repo channel involvement comes from **`serves_channels`** (graph blast-radius), which is honest
  and complete **today** — clearly distinct from "owns a channel".
- **No vendor** in the sheet → vendor-level precision still rides `delivery_topology.json` + name tokens
  + the (future) full message map. A separate vendor sheet, if it ever exists, would slot in the same way.
- `Remark` is free text → treat as sensitive; never surface verbatim in committed/relayed artifacts.
