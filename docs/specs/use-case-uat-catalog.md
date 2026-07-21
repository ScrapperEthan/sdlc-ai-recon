# Spec (Sonnet 5 builds; author Opus 4.8) — UAT Use Case catalog: P0 correctness + 3-table ingest

> **Who builds: Sonnet 5, local checkout (it can push). Internal machine only pulls + verifies —
> see `RUNBOOK-45`.** The UAT export of three tables (`tbl_use_case` 2,810×63,
> `tbl_use_case_channel_rule` 6,217×42, `tbl_use_case_ext` 2,660×36; three-table join 2,630) proves the
> just-shipped Tier 0 (commit `025349f`) returns **wrong answers on UAT**: it labels UAT data
> `dev/SCT`, binds `status` to `unknown_bounce_back_status`, and computes routed/coverage from the
> **old dev/SCT** route snapshot. This spec is **Round A = P0 correctness + ingesting the two new
> tables**. The rule_text AST, Catalog/detail UI, and dynamic arch graph are **Round B** — do NOT build
> them here; they wait on box verification (`RUNBOOK-45`) and owner answers.
>
> **Hard constraints:** stdlib-only, read-only over the exports, writes only **gitignored `index/`
> artifacts**, **full backward-compat** for the existing Q&A / impact / outage / arch APIs. **Never
> commit** raw CSVs, the Word doc, PII, or person-name-bearing derived data (bank no-egress). A missing
> file stays safe (empty / `available:False`); but an **environment mismatch is NOT a plain missing** —
> it must surface as `incompatible`, never a silent mis-join.

Ground truth for all line refs below: the current `retriever/usecase_master.py` as built in `025349f`.

---

## The eight P0/P1 defects this round fixes (verified against real code)

| # | Defect | Location | Fix (block) |
|---|---|---|---|
| 1 | environment hardcoded `dev/SCT` | [usecase_master.py:137](retriever/usecase_master.py:137) | B1 |
| 2 | routed/coverage read the **old dev/SCT** route snapshot → UAT coverage wrong (PEGA 479→~87) | [usecase_master.py:180](retriever/usecase_master.py:180) | B1 + B6 |
| 3 | `status` needle matches `unknown_bounce_back_status` first | [usecase_master.py:53](retriever/usecase_master.py:53) | B2 |
| 4 | consent key `marketinginsightspushoptinflag` (plural) ≠ UAT singular `insight` | [usecase_master.py:66](retriever/usecase_master.py:66) | B2 |
| 5 | `source_system` variants split (eAlert/e-Alert, PowerCard/Power Card, MDC/mdc) | `source_systems()` | B4 |
| 6 | Disabled (`status=N`, 113) not filtered | `use_cases_for_source_system()` | B5 |
| 7 | owners use `created_by/modified_by` (maintenance fields); Ext has real business owners | `owners_for()` | B5 |
| 8 | arch pins 5 static upstreams; UAT has 154 raw + Ext.endpoint → real entry repos | `arch_nodes.json` | B7 |

---

## Building block 1 — environment/snapshot dataset (manifest-driven, single active)

**Layout** (data; gitignored — add `index/usecase-snapshots/` to `.gitignore`):
```
index/usecase-snapshots/<env>/<snapshot_id>/
  manifest.json
  tbl_use_case.snapshot.csv
  tbl_use_case_channel_rule.snapshot.csv
  tbl_use_case_ext.snapshot.csv
  tbl_event_router_usecase_topic.snapshot.csv   # OPTIONAL — only if same environment
```
**manifest.json**:
```json
{ "environment": "UAT", "snapshot_id": "20260720-1730",
  "exported_at": "2026-07-20T17:30:00+08:00",
  "tables": { "tbl_use_case": {"file":"tbl_use_case.snapshot.csv","row_count":2810},
              "tbl_use_case_channel_rule": {"file":"...","row_count":6217},
              "tbl_use_case_ext": {"file":"...","row_count":2660},
              "tbl_event_router_usecase_topic": {"file":"...","row_count":null} } }
```
**Config** (`retriever/config.py`, additive):
```python
USECASE_DATASET_DIR = _p("SDLC_USECASE_DATASET", "index", "usecase-snapshots", "active")
```
**New module `retriever/usecase_catalog.py`** owns a `Dataset` abstraction:
- `active_dataset()` → loads `USECASE_DATASET_DIR/manifest.json`. **Back-compat:** if no manifest dir
  exists but the legacy `USECASE_MASTER_CSV` file does, synthesize a one-table dataset with
  `environment = os.environ.get("SDLC_USECASE_ENV", "unknown")` (NOT `dev/SCT`).
- `snapshot_manifest()` returns the real `environment`/`snapshot_id`/`exported_at`/`row_count` **from the
  manifest** — kills defect #1. Keep the `production_verified:false` + caveat fields.
- **Cross-environment join guard.** The route dimension (`has_route`, routed counts) is computed **only
  when this dataset's own manifest declares `tbl_event_router_usecase_topic`** (same environment). If it
  does not, route-based coverage is `{"available": false, "reason": "no same-environment route
  snapshot"}` and coverage falls back to the funnel in B6 — it must **never** reach across to the
  dev/SCT `config.USECASE_SNAPSHOT_CSV`. This kills defect #2.

**`retriever/usecase_master.py` becomes a thin facade** re-exporting the existing public names
(`master_for`, `consent_preflight`, `use_cases_for_source_system`, `source_systems`, `owners_for`,
`quality_report`, `snapshot_manifest`) delegating to `usecase_catalog`, so `impact_report.py`,
`outage_report.py`, `webapp/tools.py`, `mcp_server.py`, `retrieval_service.py` need no import churn.
Existing Tier-0 tests that assert the buggy behavior (`environment == "dev/SCT"`, old routed counts) are
**updated** — they encoded the bug.

## Building block 2 — column binding: exact → alias → unique-fuzzy → ambiguity

Replace the "first column containing the needle" logic. `resolve_column(cols, field)`:
1. **exact** normalized match against a per-field set of accepted flattened names;
2. explicit **alias map** entry;
3. fuzzy needles **only if exactly one** column matches;
4. `>1` candidate → bind `None`, record in `column_bindings.ambiguous[field] = [candidates]` — never
   silently pick one.

Accepted names must include `status` → `status` (exact wins over `unknownbouncebackstatus`, defect #3),
and consent `marketing_insight_push_optin_flag` → accept **both** `marketinginsightpushoptinflag` and
`marketinginsightspushoptinflag` (defect #4). Emit the resolved `column_bindings` (bound + ambiguous)
into the manifest echo and the quality report. **Add a real UAT header contract test** (all 63/42/36
headers) asserting these two bindings.

## Building block 3 — ingest `tbl_use_case_channel_rule` + `tbl_use_case_ext`

In `usecase_catalog.py`, read-only, missing-file-safe, `utf-8-sig`, 1-based line citations
(`<relpath>:<line>`):
- `rules_by_use_case_id()` → `{uc_id: [rule, …]}`; each rule keeps `channel, priority, route, router,
  traffic_percentage, tag, sender, send_policy, status, citation`.
- `ext_by_use_case_id()` → `{uc_id: ext}`; keep `service_line, messaging_service_level, delivery_mode,
  endpoint, rule_text, message_owner, business_contact, business_team, team_head, depart_head,
  cost_owner, signoff_by, downstream_name, is_dual_channel, support_dual_vendor,
  regulatory_requirement, high_risk_flag, citation`.
- **Channels for a use case (Round A = fact, no parsing):** `sorted(distinct rule.channel)`. Store
  `rule_text` raw only; **do NOT parse it** (that is Round B's AST). `delivery_mode` is a **string**
  (`REALTIME/BATCH/TIMECRITICAL`) in UAT — do not assume the smallint the Word doc lists. Ext missing
  the Word-only `dormant_period` column is a **schema-drift warning, not a crash**.

## Building block 4 — `source_system` canonicalization

`canonicalize_source_system(raw)` → `{canonical, display_name, raw}`:
1. trim; 2. casefold; 3. remove all non-alphanumerics for the **canonical key** (`"e-alert"→"ealert"`,
`"power card"→"powercard"`); 4. `index/source_system_aliases.json` override may map a canonical key to a
preferred `display_name` and fold extra variants. **Do not auto-merge semantically distinct names** —
casefold+strip keeps `mdc` ≠ `mdctest`, so `MDC` and `MDC Test` stay separate; anything beyond pure
format/case folding requires an explicit alias entry.

`source_systems()` now returns
`{canonical, display_name, raw_variants:[…], use_case_count, active_count}` ordered by count desc.

## Building block 5 — active filter + layered owners

- **Active filter:** with `status` correctly bound (B2), `active = status.upper()=="Y"`.
  `use_cases_for_source_system(..., include_inactive=False)` **defaults to Active only**; every response
  carries `active_count` / `inactive_count`, and each item carries `active: bool` (UI red badge for N).
- **Owners layered** (defect #7) — `owners_for(use_case_ids)` returns three groups, distinct non-empty:
  - `business_owners`: `message_owner, business_contact, business_team, team_head, depart_head` (Ext)
  - `cost_governance`: `cost_owner, signoff_by` (Ext)
  - `config_maintainers`: `created_by, modified_by` (master)
  This is the **change-notification** list — now business-accurate, serving the flagship
  impact-notification ask. Missing Ext → only `config_maintainers` populate (no crash).

## Building block 6 — coverage funnel (replaces routed/catalog-only)

`source_system_coverage(source_system)` — UAT-native readiness, **not** the old route join:
```
{ canonical, display_name, total, active,
  configured:          #UCs with ≥1 channel rule,
  expression_ready:    #UCs with non-blank ext.rule_text,
  entrypoint_traceable:#UCs with ≥1 ext.endpoint segment resolving to a known repo (B8),
  catalog_only:        #UCs master-only, no rule AND no ext }
```
Terms per the analysis §10: `configured` / `expression_ready` / `entrypoint_traceable` / `catalog_only`.
`topic_traceable` and `delivery_traceable` need router→topic enums + the AST → **Round B**. Each member
in `use_cases_for_source_system` carries these per-UC flags (plus `has_route` **only** when a same-env
route snapshot exists per B1; otherwise omit it). The confidence banner states which stages are covered.

## Building block 7 — endpoint → repo resolver (Round A; data/resolver only, no new UI)

`resolve_endpoint(endpoint_raw)` → `[{raw, repo, confidence}]`:
- split on `->`; per segment trim; **skip version tokens** (`v1..v9`, standalone `v\d+`, `->v3->`) →
  annotate as `version_annotation`, never a repo named `v3`;
- match segment against the repo universe `config.REPOS_TXT` (`recon_out/repos.txt`): exact →
  `declared-exact`; case/hyphen-normalized → `declared-normalized`; else `unresolved` (**keep the raw
  evidence**). UAT reference: 1,674/1,678 non-blank endpoints resolve; top entry
  `mc-hk-hase-ingress-api` ×1,187.
- This upgrades `source_system → generic ingress` into evidence-backed
  `source_system → declared endpoint repo(s) → use_case`. Surface resolved repos in the use-case and
  source-system reports with `confidence`. Dynamic arch **rendering** of these edges is Round B; Round A
  only produces the resolved data + confidence.

## Building block 8 — performance / response-size guard

Module-level cache keyed on `(path, mtime, size)` per CSV; build once: `by_use_case_id`,
`by_source_system` (canonical), `rules_by_use_case_id`, `ext_by_use_case_id`; invalidate on signature
change. Add `offset`/`limit` to `use_cases_for_source_system` and the `source_system_impact` tool;
default to **aggregate + top-N examples** (MDC ≈ 880 UCs would otherwise overflow LLM context / UI).

## Building block 9 — wire into existing entry points (additive, back-compat)

- `impact_report.build_usecase_report` + `build_source_system_report`: layered owners (B5), coverage
  funnel (B6) instead of routed/catalog-only, channels-from-rules (B3), endpoint repos (B7). Keep
  null-safe (master absent → today's output).
- `outage_report.affected_use_cases`: add name / source_system / business owner.
- `webapp/tools.py`: `source_system_impact` + `list_source_systems` gain `include_inactive`, `offset`,
  `limit`; `list_source_systems` returns canonical + `raw_variants` + active/inactive counts. Mirror in
  `mcp_server.py` and `retrieval_service.py`. Every response carries `{environment, snapshot_id,
  source_tables, production_verified:false, citations}` from the manifest.

## Building block 10 — quality report on the new model

Extend `quality_report()`: emit `column_bindings` (bound + ambiguous); replace the old join-coverage
block with the funnel counts + `active/inactive`; keep illegal `business_category` (now must flag **33
and 37** on UAT), missing `source_system`, stale, junk work_stream; add `route_dimension:
available/unavailable(reason)`. Stays wired into `refresh.py` → `index/reports/USECASE_QUALITY.{md,json}`
(gitignored). Missing dataset → `available:false`, returncode 0.

## Tests (fixtures; extend the existing suite)

- **header/schema:** UAT 63/42/36 header contract; `status`→`status` (not `unknown_bounce_back_status`);
  singular `marketing_insight_push_optin_flag` detected; `delivery_mode` string values; Ext missing
  `dormant_period` → warning not crash.
- **environment:** UAT master + a SIT/dev route snapshot → route join **refused** (`incompatible` /
  route unavailable), never a silent count; provenance shows `UAT`; same-`use_case_id` across SIT/UAT
  don't overwrite.
- **canonicalization:** `eAlert/ealert/EAlert/E-alert/e-Alert` → one canonical; `PowerCard/Power Card` →
  one; `MDC Test` **not** merged into `MDC`; `_v4` / standalone `v3` treated as version annotation.
- **endpoint:** exact repo match; unresolved keeps raw evidence.
- **scale/UI:** MDC-880 fixture → pagination (`offset`/`limit`) works; disabled excluded by default;
  `include_inactive=true` includes them with `active:false`.
- **regression:** master/dataset absent → byte-identical to today's null-safe behavior.

## Honesty / limits (in reports + `PROJECT-STATE.md`)

UAT is a **snapshot**, `production_verified:false`. Round A channels come from the **`channel_rule.channel`
column (fact)**; the initial/parallel/fallback **decision tree is NOT computed yet** (Round B AST). Coverage
is `configured/expression_ready/entrypoint_traceable/catalog_only` — say which stages a number covers;
never present `configured` as "reaches the customer." `is_dual_channel`/`support_dual_vendor` are
**declared flags**, not computed facts.

## Round B (next spec, after `RUNBOOK-45`) — do NOT build now

rule_text tokenizer + AST (EBNF in analysis §6.1); channel-set consistency validation (the 9 mismatches);
full funnel (`topic_traceable`/`delivery_traceable` via router→topic enums); dynamic source/endpoint arch
focus with edge-confidence tiers; Use Case Catalog + detail drawer UI (server-side pagination); quality
dashboard. **Round C:** `tbl_use_case_router`, AEM template lineage, department mapping, `ext_2way`
two-way graph, SIT⇄UAT snapshot diff.

## Not this repo's job (route to owners via `RUNBOOK-45`)

The analysis §12 MDC **runtime** risks (UseCaseService `findById` no status filter; MessageDirectorService
rule_text null guard; null-priority comparator NPE; PUSH+INBOX no director; entity schema drift) are for
the MDC/runtime owner. We make the analysis **see and report** them; we do **not** modify product runtime
code.

## Round A — post-verification follow-ups (RUNBOOK-45 Part A, box run on real UAT, build `143e6b5`)

Part A **PASSED on real UAT**: env=UAT; `status`→`status`; **cross-env route join refused** (no
297/56/2513); PEGA `configured=479` (not ~87); canonicalization / active-filter / endpoint-resolver /
pagination all correct; illegal cats 33+37; insight-consent Y=3; CSV SHA-256 matches source; no person
names exposed (owner detail kept in a gitignored `*.local.md`). The data layer is verified. Four fixes
remain before Round A is "done":

1. **Test isolation (P0 — CI correctness).** With `SDLC_USECASE_DATASET` set to the real UAT dataset,
   14/243 tests fail (`test_usecase_master` 8, `test_source_system_report` 4,
   `test_refresh_usecase_quality` 2): the fixtures patch the legacy snapshot path, but the ambient env var
   wins, so tests read the real 2,810-row data (expect PEGA=2, get 479). **Not a data bug.** Every test
   that builds a fixture dataset must pin its own env — `unittest.mock.patch.dict(os.environ, {...})` over
   `SDLC_USECASE_DATASET` (and/or legacy `USECASE_MASTER_CSV`), restored in tearDown. Then **re-run WITH
   the env var set and confirm 243/243**, to prove no real regression hid among the 14.

2. **`junk_work_stream` over-flags (quality accuracy).** UAT flags 2,405/2,810 (85%) because the heuristic
   treats `value.isdigit()` as junk. If `work_stream_name` is legitimately a numeric project id this is a
   false positive. Separate sentinel junk (`invalid/test/n-a/…`) from numeric ids; stop counting pure
   numerics as junk unless a Part-B owner confirms they are meaningless. (Also a Part-B question.)

3. **CLI markdown drops the endpoint repo names (the upstream win).** Structured output carries repo +
   confidence, but the markdown renderer prints only `entrypoint_traceable=True`. Render the resolved
   endpoint repo(s) + confidence in the use-case / source-system markdown — that repo name is the payload.

4. **CLI has no `offset/limit`.** Pagination only reached `webapp.tools.source_system_impact`; a direct
   `impact_report.py source-system:MDC` dumps all ~880. Add the same default top-N cap (+ an "N of M"
   note) to the CLI path.

Minor (optional): the global quality funnel reports `configured/expression_ready` over all master rows
(2,784 / 2,637); the readiness story reads better scoped to the 2,697 Active (as analysis §5.2 did).

**Still outstanding: RUNBOOK-45 Part B** — the business/data + runtime owner questions were not part of
this box run; they remain open and steer Round B (rule_text grammar especially).
