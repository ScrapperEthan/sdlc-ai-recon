# Spec (INTERNAL Codex builds; Sonnet 5 implements) — Use Case master data + upstream `source_system`

> **Who builds: Sonnet 5 on the box. Author: Opus 4.8.** Verify with a follow-up RUNBOOK.
> **The need:** a new export `tbl_use_case.csv` (1,175 use cases × 63 fields) is the **Use Case master
> data** — keyed on the same `use_case_id` as our existing routing snapshot. It carries the one layer
> our chain has always been missing: the **upstream business system** (`source_system`), plus business
> identity, ownership, and policy switches. This spec wires it in so `build_usecase_report` stops
> returning a bare id, and adds `source-system:` as a **new impact entry point**.
>
> **Hard constraints (same as every spec here):** stdlib-only, read-only over the export, writes only
> **gitignored `index/` artifacts**, **do not break the Q&A app or existing reports**. Carry the
> **snapshot discipline verbatim** from `retriever/messages.py` — environment label, `exported_at`,
> per-line citations, and the rule *"absence in this dev/SCT export ≠ absence in production."*
>
> **Scope boundary — Tier 0 only.** Build blocks 1–5 below. The **actual channel chain, priority, and
> bounce-back fallback are NOT in this table** — they come from `tbl_use_case_channel_rule` (arriving
> separately). Do **not** derive a channel list from the opt-in flags. Tier 1 is called out at the end.

---

## Background — this is our second snapshot; the value is the JOIN

We already load `index/tbl_event_router_usecase_topic.snapshot.csv` (`use_case_id → topic`, dev/SCT) in
`retriever/messages.py`. That is the async "last mile." `tbl_use_case.csv` is a **second snapshot on the
same primary key** and supplies the **left end** of the chain:

```
        (NEW: tbl_use_case)              (existing: routing snapshot)   (existing: message_edges)
source_system → Use Case (identity+policy) → topic → producer/consumer repo → channel → vendor
```

`source_system` appears **nowhere** in the codebase today (grep confirms 0 hits). This is net-new, not a
refactor.

### Three populations (each is handled differently — do not blur them)

Joining the two snapshots on `use_case_id` yields three groups. Build must treat them distinctly:

| Population | ~count | Has a topic route? | Treatment |
|---|---|---|---|
| In routing snapshot **and** master | **325** | yes | enrich existing use-case reports (block 2) |
| In routing snapshot, **not** in master | 28 | yes | quality alarm: "orphan route" (block 5) |
| In master, **no** route in snapshot | **850** | no | catalog / `source-system` listing only — **never** presented as channel-traceable |

Concretely: a `source-system:PEGA` blast radius must split into **"has known route → traceable to
channels"** vs **"cataloged only → business impact, no traced channel."** Honesty here is a design
requirement, not a nicety.

---

## Fields this spec uses (self-contained subset of the 63)

Full field analysis: `CSV字段分析说明_图片提取版.md` (internal Codex). Tier 0 only needs these:

**Identity / governance**
`use_case_id` (PK, join key) · `use_case_name` · `project_name` · `source_system` · `work_stream_name`
· `line_of_business` (WPB/CMB) · `business_category` (enum below) · `country_code` · `group_member`
(HASE/HSBC) · `app_name` · `created_by` · `created_time` · `modified_by` · `last_modified_time` ·
`status`

**Consent preflight (policy switches — "check this consent before sending", NOT "uses this channel")**
`marketing_optin_flag` · `push_optin_flag` / `marketing_push_optin_flag` / `high_risk_push_optin_flag` /
`securities_push_optin_flag` / `marketing_insights_push_optin_flag` · `sms_optin_flag` /
`marketing_sms_optin_flag` · `email_optin_flag` / `marketing_email_optin_flag` · `mms_optin_flag` /
`marketing_mms_optin_flag` · `wechat_optin_flag` / `marketing_wechat_optin_flag` · `whatsapp_optin_flag`
/ `marketing_whatsapp_optin_flag`

**`business_category` enum** (seed this as an inline dict; the source of truth is `BusinessCategoryEnum.java`
on the mirror — the quality check below flags any CSV code missing from this dict, which is how `33` gets
caught):

```
0 WPB_REALTIME_MARKETING   1 WPB_REALTIME_SERVICING   2 WPB_BATCH_SERVICING   3 WPB_BATCH_MARKETING
4 WPB_HR_REALTIME_SERVICING 5 WPB_SEC_REALTIME_SERVICING 6 CMB 7 WPB_HS_REALTIME_SERVICING
8 WPB_TC_REALTIME_SERVICING
10 HASE_WPB_SERVICING_REALTIME_GENERAL   11 HASE_WPB_SERVICING_REALTIME_HIGHRISK
12 HASE_WPB_SERVICING_BATCH   13 HASE_WPB_MARKETING_REALTIME_GENERAL   14 HASE_WPB_MARKETING_BATCH
15 HASE_CMB_SERVICING_REALTIME_GENERAL   16 HASE_CMB_SERVICING_REALTIME_HIGHRISK
17 HASE_CMB_SERVICING_BATCH   18 HASE_CMB_MARKETING_REALTIME_GENERAL   19 HASE_CMB_MARKETING_BATCH
20 HASE_WPB_SERVICING_TIMECRITICAL   21 HASE_CMB_SERVICING_TIMECRITICAL
32 HSBC_WPB_SERVICING_BATCH   33 <<MISSING FROM ENUM — flag as data-contract drift>>
34 HSBC_WPB_SERVICING_TIMECRITICAL   35 HSBC_WPB_SERVICING_REALTIME_HIGHRISK
```

Column names must be **detected**, not hardcoded to a fixed index — reuse the `_detect(cols, *needles)`
helper pattern from `messages.py` (lower-case, strip `_`, match all needles) so a renamed header
(`useCaseId` vs `use_case_id`) still binds.

---

## Building block 1 — ingest: config + `retriever/usecase_master.py`

**Config** (`retriever/config.py`, additive):
```python
USECASE_MASTER_CSV = _p("SDLC_USECASE_MASTER", "index", "tbl_use_case.snapshot.csv")
SOURCE_SYSTEM_ALIASES_JSON = _p("SDLC_SOURCE_SYSTEM_ALIASES", "index", "source_system_aliases.json")
```
Add `index/*.snapshot.csv` to `.gitignore` (data export, never commit — matches how the routing
snapshot lives only on the box; bank no-egress).

**New module `retriever/usecase_master.py`** — read-only, stdlib, missing file → empty/`available:False`
(never crash). Mirror the exact provenance + citation helpers already in `messages.py`
(`_snapshot_manifest`, `_snapshot_citation`, `utf-8-sig`, 1-based line = row 2 for first data row).
Public surface:

- `master_for(use_case_id) -> dict | None` — the joined identity for one id: `{use_case_id, name,
  project, source_system, work_stream, line_of_business, business_category_code,
  business_category_label, country, group_member, app, created_by, created_time, modified_by,
  last_modified_time, status, citation}` where `citation = "index/tbl_use_case.snapshot.csv:<line>"`.
  Unknown `business_category` → label `UNKNOWN(<code>)`.
- `consent_preflight(use_case_id) -> {"checks":[{consent, flag_value, citation}], ...}` — only the
  opt-in flags that are `Y`, each labelled ("Marketing Consent", "Marketing Push", "SMS", …).
  **Docstring must state: these are pre-send consent checks, NOT the channel list.**
- `use_cases_for_source_system(source_system, limit=None) -> {available, source(manifest), total,
  returned, truncated, items:[{use_case_id, name, project, has_route(bool), citation}]}` — case-
  insensitive match on the trimmed value, folded through `source_system_aliases.json` if present
  (`{"PEGA": ["Pega","PEGA_HK"]}`). `has_route` = the id also appears in the routing snapshot.
- `source_systems() -> [{source_system, use_case_count, routed_count}]` — distinct list for the
  `source-system` picker and the arch upstream nodes.
- `snapshot_manifest()` — same envelope shape as `messages._snapshot_manifest()` but
  `source_table: "tbl_use_case"`.
- `quality_report()` — see block 5.

Provenance envelope every consumer surfaces: `{environment: "dev/SCT", source_table: "tbl_use_case",
exported_at, row_count, production_verified: false, caveat: "…absence here does not prove absence in
production."}`.

---

## Building block 2 — enrich the use-case report (identity + governance + consent)

`impact_report.build_usecase_report` (**[impact_report.py:485](impact_report.py:485)**) today sets
`description = use_case_id` (**[impact_report.py:527](impact_report.py:527)**). Replace the `target`
block and add two sections. **Additive and null-safe:** when the master snapshot is absent, the report
must be byte-identical to today (fall back to `description = use_case_id`).

```
target.description  -> f"{use_case_id} — {name}"  (fall back to id if no master row)
target.business     -> {source_system, project, work_stream, line_of_business,
                        business_category (code+label), country, group_member, app, citation}
target.governance   -> {created_by, created_time, modified_by, last_modified_time, status,
                        stale: last_modified_time older than 12 months (or never modified), citation}
report.consent_preflight -> usecase_master.consent_preflight(...)  # labelled "policy, not routing"
```

This is the "`M2050` → `M2050 / Max_Use_Case_03, 来源 MDC, WPB, 高风险实时服务, 路由至 …`" upgrade — smallest
diff, largest payoff. Every added value carries the master-row citation. Mirror the same enrichment into
`outage_report.affected_use_cases` items (**[outage_report.py:202](outage_report.py:202)** returns id +
topic only) so the outage view also shows name + source_system + owner.

---

## Building block 3 — `source-system:` as a new impact entry point

**Parse.** Add `source-system` to `impact_report.parse_target`
(**[impact_report.py:15](impact_report.py:15)**, currently `{"topic","use-case"}`) and dispatch a new
`build_source_system_report(value, tags)` from `build_report`
(**[impact_report.py:547](impact_report.py:547)**).

**Compose** (aggregate the members — reuse existing primitives, don't reinvent):
1. `members = usecase_master.use_cases_for_source_system(value)`.
2. **Split** members into `routed` (`has_route`) and `catalog_only`.
3. For `routed`, union their topics via `messages.usecase_route(use_case_id)`, then their
   producer/consumer repos via `messages.who_produces/who_consumes` (same fan-out
   `build_usecase_report` already does), dedup with the existing `route_signature`.
4. `channels` = `channel_chain(...)` over that participant/topic set (existing helper).
5. `owners` = distinct `created_by`/`modified_by` across members (the **change-notification** list —
   directly serves the flagship "修改会通知哪些负责人" ask).

**Envelope** (parallel to `outage_report.build_report`, every claim cited):
```json
{ "target": {"input":"source-system:PEGA","kind":"source-system","value":"PEGA",
             "use_case_count":N,"routed_count":R,"catalog_only_count":N-R},
  "use_cases": {"routed":[…], "catalog_only":[…]},
  "topics": […], "async_routes": […], "channel_chain": […],
  "upstream": […], "downstream": […], "owners": […],
  "confidence_banner": "渠道影响仅覆盖 R/N 个有路由快照的 Use Case；其余 N-R 个仅有业务登记，无法追踪到渠道。",
  "citations": […] }
```

**Expose it** in the three entry points that already share `dispatch`:
- `webapp/tools.py`: add a `source_system_impact` tool (schema + a branch in `dispatch`,
  **[webapp/tools.py:110](webapp/tools.py:110)**) — description keyed to "PEGA/上游系统 出问题会影响哪些
  Use Case / 渠道 / repo", "L400 接入了哪些流程". Also add `list_source_systems`
  (→ `usecase_master.source_systems()`) for the picker.
- CLI: `python impact_report.py source-system:PEGA` works via the new parse branch.
- `mcp_server.py` / `retrieval_service.py`: additive endpoint mirroring the CLI (same shape).

---

## Building block 4 — arch map: business-upstream layer (per-selection, not permanent)

`static/arch_nodes.json` today starts at column 0 = **technical ingress adapters** (DSP/File/MQ/Kafka).
The **business** upstream (PEGA/eAlert/L400/…) is a layer to their left. Do **NOT** draw all ~80 source
systems on the overview (clutter) — light them **on selection**, reusing the existing
`arch_focus`/`computeHighlight` mechanism.

Least-invasive change (does **not** renumber the 7 columns):
- Add an auxiliary array to `arch_nodes.json`: `"business_sources": [{"id":"biz-pega","label":"PEGA",
  "source_system":"PEGA"}, …]` — seed from `usecase_master.source_systems()` top entries
  (PEGA/MDC/eAlert/HCC/L400). `hidden_by_default: true`.
- Edge is **generic** for now: `business-source → ingress-api`. The precise `source_system → DSP/File/
  MQ/Kafka` adapter split is **not known from this table** — leave it generic and override-able via
  `index/arch_map.override.json`; do not invent per-adapter edges. (This is a Tier 1 refinement.)
- `retriever/arch_focus.py`: extend `focus(kind, value)` to accept `kind="source-system"` (and
  `kind="use-case"`, resolving the id's `source_system` via `usecase_master.master_for`). Return the
  business-source node id in `affected_node_ids` plus the downstream chain, so the inline diagram lights
  the upstream node on demand. `static/arch.html` renders `business_sources` in a slim left gutter only
  when such a focus is active.

Keep it honest: the gutter node means "this use case's declared upstream system," cited to the master
row — not a discovered code edge.

---

## Building block 5 — data-quality / consistency report

`usecase_master.quality_report()` returns counts **plus a few example ids** for each:
- **join coverage:** master∩routing (325), routing-only orphans (28), master-only no-route (850).
- **missing `source_system`** (~13.9%).
- **stale:** `last_modified_time` (fallback `created_time`) older than 12 months (~79%).
- **illegal enum:** `business_category` codes not in the seed dict (catches `33`) → "data-contract drift".
- **test / junk `work_stream_name`:** values like `invalid`, `1`, a person's name.
- **`status` uniformity:** all `Y` → note "export may be pre-filtered; cannot infer active/inactive."

Wire into `refresh.py` as an additive step (**[refresh.py:158](refresh.py:158)** `refresh()`), writing
`index/reports/USECASE_QUALITY.md` + `.json` (both gitignored) and appending a `steps` entry — same
pattern as the existing `TAG_RECONCILE` step. There is **no `deploy/preflight.py`** in the repo; if one
is added later, call `quality_report()` there too, but `refresh.py` is sufficient for now. Missing
snapshot → the step reports "master snapshot absent" and returncode 0 (not a failure).

---

## Tests (fixtures, matching the existing style under `tests/`)

- **ingest/join:** a 3-row master fixture + a 2-row routing fixture → `master_for` returns identity +
  citation; `use_cases_for_source_system("PEGA")` splits `has_route` correctly; missing file →
  `available:False`, no crash.
- **enum:** `business_category` `33` → `UNKNOWN(33)` and appears in `quality_report().illegal_enum`.
- **enrichment:** `build_usecase_report` with master present → `target.business.source_system` populated
  and cited; **with master absent → output identical to today** (regression guard).
- **source-system report:** `build_source_system_report("PEGA")` → `routed`/`catalog_only` split, owners
  deduped, confidence banner present, every item cited.
- **consent:** only `Y` flags surface; docstring/contract asserts "not a channel list."
- **tools/CLI:** `parse_target("source-system:PEGA")` → `("source-system","PEGA")`; unknown value →
  clean error, not a stack trace.

---

## Honesty / limits — put in the reports AND in `PROJECT-STATE.md`

- Everything from this table is a **dev/SCT snapshot**; carry the "absence ≠ absence in prod" caveat.
- `source-system` channel impact only covers the **routed** members; catalog-only members are business
  registrations with **no traced channel** — say so in the banner, never pad the blast radius with them.
- Consent/opt-in flags are **pre-send checks, not the channel list**.

## Tier 1 — deferred until `tbl_use_case_channel_rule` arrives (do NOT build now)

The **real** channel list, priority, router/route, `traffic_percentage`, sender, `send_policy`, and the
**bounce-back fallback chain** live in `tbl_use_case_channel_rule` (columns: `channel, priority, route/
router, traffic_percentage, sender, send_policy, status`). When it lands:
1. Replace the topic-name-inferred channel chain with the **rule-driven** per-use-case chain.
2. Build the **bounce-back fallback** (`bounce_back`, `bounce_back_next_channel`, per-channel periods /
   max-periods from `tbl_use_case`) into the outage view: "how many use cases auto-fall-back, to which
   channel, after how many minutes, which have no backup."
3. Resolve the precise `source_system → ingress adapter` edges in the arch map.
Also Tier 1: the **Use Case Catalog** page (searchable/filterable over all 1,175 — the natural home for
the 850 route-less use cases and the ~218 near-duplicate-name "use-case families").

## Open questions to confirm before/while building (ask the data owner)

1. **`source_system` — controlled vocabulary or free text?** #1 blocker for reliable `source-system:`
   aggregation; if free text, populate `source_system_aliases.json`.
2. **Which environment** is this CSV (Prod / SCT / Dev)? Sets the provenance label.
3. **Is `use_case_id` stable** across the two snapshots and across environments? The whole join rides
   on it.
4. `business_category=33` — new category or bad data? (drift alarm either way.)
