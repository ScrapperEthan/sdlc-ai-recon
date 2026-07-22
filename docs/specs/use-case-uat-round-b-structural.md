# Spec (Sonnet 5 builds; author Opus 4.8) — UAT Use Case Round B (structural, owner-independent)

> **Who builds: Sonnet 5, local checkout. Verify with a follow-up RUNBOOK.** Round A (`143e6b5`,
> verified on real UAT — see `docs/specs/use-case-uat-catalog.md`) made UAT ingest correct and read the
> three tables. This spec is **Round B's owner-independent structure**: the rule_text AST, the
> multi-source consistency validator, the Use Case Catalog/detail UI, and the dynamic source→endpoint
> arch focus. Everything here is buildable **without** waiting on business/runtime owner answers, because
> the one genuinely owner-gated thing — what the rule_text operators operationally *mean* — is built as a
> **config scaffold that defaults to "flag, don't guess."**
>
> **Why "flag, don't guess" is mandatory (RUNBOOK-45 Part B evidence):** for `I0141`/`I0142` the rule_text
> `LETTER > (EMAIL & SMS)` **disagrees with** the `channel_rule` priority order (LETTER=1/EMAIL=2/SMS=3)
> **and** with the Portal composer (`LETTER > EMAIL > SMS`), and the current runtime parser is itself buggy
> (`contains("\\|")` matches a literal backslash; the Decision parser yields `[LETTER, SMS]`). So we must
> NOT hardcode "`&`=parallel, `>`=fallback" as truth, and must NOT treat the runtime as ground truth.
>
> **Hard constraints (unchanged):** stdlib-only backend, read-only over the snapshots, writes only
> gitignored `index/` artifacts, **full backward-compat** with the Q&A / impact / outage / arch APIs and
> Round A. Never commit CSVs / PII. Every response keeps the Round A provenance envelope
> (`{environment, snapshot_id, source_tables, production_verified:false, citations}`).

**Recommended landing order:** B1→B2→B3 (backend logic) as one commit set, then B4→B6 (UI) as a second,
then B7 + tests. Each is additive.

---

## B1 — rule_text tokenizer + AST (structural only, NO operational meaning asserted)

New module `retriever/rule_text.py` (stdlib). Grammar (from the UAT analysis; operator counts on real
data: `>` 310, `&` 193, `|` 52, mixed 108, no-operator 1,977, non-blank 2,640):
```ebnf
expr        ::= selectable
selectable  ::= fallback ("|" fallback)*
fallback    ::= parallel (">" parallel)*
parallel    ::= atom ("&" atom)*
atom        ::= CHANNEL | "(" expr ")"
```
`parse(rule_text)` → structural AST:
```json
{ "mode": "SINGLE|PARALLEL|FALLBACK|UPSTREAM_SELECTED|MIXED|EMPTY",
  "channels": ["LETTER","EMAIL","SMS"],
  "operator_tree": { "op": ">", "left": {"channel":"LETTER"},
                     "right": {"op":"&","left":{"channel":"EMAIL"},"right":{"channel":"SMS"}} },
  "normalized_expression": "LETTER > (EMAIL & SMS)",
  "semantics": "unconfirmed",
  "parse_warnings": [] }
```
- Channel vocabulary is the DB set (SMS/EMAIL/PUSH/LETTER/WHATSAPP/WECHAT/MMS/TWOWAYSMS/INAPP/PUSH_INBOX);
  an unknown token is a `parse_warning`, never a crash. Blank/None → `mode:"EMPTY"`.
- **Do NOT emit `initial_channels` / `fallback_edges` as fact.** Those are an *interpretation* of the tree
  and depend on operator semantics (B2). The AST carries only the structural tree + `semantics:"unconfirmed"`.
- `parse_warnings` covers: unknown channel, duplicate channel token, unbalanced parens, empty, and
  literal-escape artifacts (so we can spot the same class of bug the runtime `contains("\\|")` has).

## B2 — operator-semantics config (owner-confirmed; safe default)

Override file `index/rule_text_semantics.json` (config path in `retriever/config.py`), read-only,
missing → the safe default:
```json
{ ">": {"meaning": "unconfirmed"}, "&": {"meaning": "unconfirmed"}, "|": {"meaning": "unconfirmed"},
  "precedence": ["|", ">", "&"], "confirmed_by": null, "confirmed_at": null }
```
`interpret(ast, semantics)` returns `initial_channels` / `parallel_groups` / `fallback_edges` /
`selectable_channels` **only when the relevant operators are confirmed**; while `unconfirmed`, it returns
`{"available": false, "reason": "operator semantics not owner-confirmed"}` and every UI/report shows the
**structural** tree with an "语义待 owner 确认 / semantics unconfirmed" badge. When the owner fills the file
in (e.g. `">":{"meaning":"sequential_fallback"}`, `"&":{"meaning":"parallel_send"}`), interpretation lights
up with zero code change. This is the single seam the owner answer plugs into.

## B3 — multi-source consistency validator + fold Part B findings

New `retriever/usecase_consistency.py` (or extend `usecase_catalog`). Per UC, compare the **three
data-computable** sources and tag disagreements with both source citations:
1. `rule_text` channel set (from B1 AST) **vs** `channel_rule.channel` distinct set (Round A fact) →
   `channel_set_mismatch`.
2. `rule_text` operator structure **vs** `channel_rule.priority` ordering → `expression_vs_priority`
   (the `I0141`/`I0142` canonical case: rule_text groups EMAIL&SMS but priority is strictly 1<2<3).
3. rule_text internal validity → `duplicate_channel` / `unknown_channel` / `blank_with_rules`.

The Portal composer / runtime parser is a **4th, code-only source** we cannot compute per-UC from data;
record the known `I0141`/`I0142` divergence as a documented caveat, do not fabricate a per-UC composer result.

**Fold in the Part B quality findings** (each with example ids, severity, citations): orphan channel-rule
UCs (`C5501`,`W9992`), orphan Ext UCs (`A2040`,`C1501`,`C5501`,`M1780`), 26 active UC with no channel rule
(13 also blank rule_text), 154 master missing Ext (99 active/55 inactive), `business_category` drift (33 →
`B0001`/L400/LETTER; 37 → `I0135-I0140`/MDC/EMAIL — stay `UNKNOWN(code)` until owner names them),
null-priority (`W8765/W9992/W9994/W9996`), PUSH+INBOX (`A0027/M0016/M0018/M0019/M0020/M0022/M0023` — rule
present, no router/Ext/known Director). Emit a severity-ranked list; **flag, never silently pick a winner.**

## B4 — Use Case Catalog + detail drawer (server-side paginated)

New page `webapp/static/catalog.html` + routes. **2,810 rows → server-side pagination**, never inject the
full set into the frontend.
- **Catalog:** columns/filters `use_case_id / name / source_system(canonical) / Active|Disabled /
  service_line / delivery_mode / channel(s) / business_category / endpoint repo / owner-team / validation
  severity`. Default Active-only (Round A `include_inactive` flag). Disabled → red badge.
- **Detail drawer, 6 sections** (analysis §11.3): (1) Identity & status; (2) Source system & endpoint
  repo(s) [Round A resolver + confidence]; (3) **Channel decision tree** — render the B1 structural
  operator tree; while `semantics:"unconfirmed"`, draw the operator edges labelled by symbol (`>`/`&`/`|`)
  with the "semantics unconfirmed" badge, NOT as asserted parallel/fallback; (4) Router/topic/job/vendor
  delivery path [what Round A + topology can show today; unknown segments marked unverified]; (5) Business
  owner / governance / regulatory [Round A layered owners]; (6) Validation findings & evidence [B3, each
  clickable to its citation].
- New backend endpoints (mirror across `webapp/server.py`, `mcp_server.py`, `retrieval_service.py`):
  `search_usecases(filters, offset, limit)`, `get_usecase(use_case_id)`, `usecase_quality(severity,
  offset, limit)`. Dataset = the Round A active manifest.

## B5 — quality dashboard

A view over B3 (analysis §11.5): a clickable, filterable list — no rule / no ext / blank-or-invalid
rule_text / expression↔channel mismatch / duplicate channel / unknown business category / missing-or-
unknown router / router-category unsupported / null-zero traffic / disabled-but-referenced / orphan
rule/ext. Each row links to the UC detail drawer + the source citation. Headline tile: **251 UC hit ≥1
risk (138 active / 113 inactive)** — labelled "risk set, not confirmed prod failures."

## B6 — dynamic source → endpoint → UC arch focus (edge-confidence tiers)

Extend `retriever/arch_focus.py` + `static/arch.html` so selecting a use-case or source-system renders the
evidence-backed chain `source_system → declared endpoint repo(s) → use_case → channel(s)` using the Round A
endpoint resolver. Missing endpoint → `generic ingress` marked **unverified** (never force an adapter). Tag
every edge with a confidence tier (analysis §9.3): `declared-db` / `configured-runtime` / `code-enum` /
`code-discovered` / `topology-derived` / `inferred-normalization`. This replaces the Round A static
5-node gutter with per-selection, cited nodes.

## B7 — cache / pagination / response-size (carry Round A discipline)

Reuse Round A's `(path, mtime, size)` index cache; add the rule/ext/AST indices. Every list endpoint
(`search_usecases`, `usecase_quality`, `source_system_impact`) takes `offset`/`limit` and defaults to
aggregate + top-N. Apply the Round A **CLI** follow-up here too if not already landed (`impact_report.py`
must also cap, not dump ~880).

## Tests (fixtures; extend the suite)

- **AST (B1):** `EMAIL > SMS`, `SMS & EMAIL`, `(PUSH > SMS) & EMAIL`, `PUSH | SMS | EMAIL`,
  `LETTER > (EMAIL & SMS)`, duplicate channel, unknown token, blank, unbalanced parens → correct
  `mode`/`operator_tree`/`parse_warnings`; **no `initial_channels`/`fallback_edges` while unconfirmed.**
- **semantics (B2):** default → `interpret` returns `available:false`; a confirmed fixture → edges appear.
- **consistency (B3):** `I0141`/`I0142` → `expression_vs_priority` flagged; channel-set mismatch fixture;
  orphan rule/ext fixtures; blank-rule_text-with-rules.
- **catalog (B4):** pagination (offset/limit); Active-only default; detail drawer builds all 6 sections;
  decision tree renders structurally with the unconfirmed badge.
- **arch (B6):** source-system focus lights endpoint repo node with confidence tier; missing endpoint →
  generic-ingress-unverified.
- **regression:** dataset absent → Round A / today's behavior unchanged.

## Explicitly DEFERRED (do NOT build here)

- **Operator operational semantics values** — B2 scaffold only; the meanings wait on the owner.
- **`business_category` 33/37 labels + final topic** — stay `UNKNOWN(code)` / drift until owner names them.
- **Deeper coverage funnel** (`topic_traceable`/`delivery_traceable` via `MessageRouterTopicEnum` /
  `TopicConfigEnum`) — needs router→topic enum extraction from the mirror; its own future task, and 33/37
  topics need owner anyway.
- **Round C tables** (`tbl_use_case_router`, `_aem_template`, `_department_mapping`, `_ext_2way`) — pending
  owner export approval.
- **MDC runtime fixes** — the 5 drafted `[MDC]` tickets are the runtime owner's, not this repo's.

---

## Separate quick change (do this too): rename **HASE assistant → MDC assistant**

Unrelated to Round B; the product is the MDC messaging assistant now. **Required (frontend-visible):**
- `webapp/static/index.html:962` — `<strong>HASE Assistant</strong>` → `<strong>MDC Assistant</strong>`
- `webapp/static/index.html:6` — `<title>HASE code assistant</title>` → `<title>MDC code assistant</title>`
- `webapp/static/index.html:960` — brand-mark `<div class="brand-mark">HA</div>` → `MD` (keep it 2-char to
  fit the existing circular mark; or `MDC` only if the CSS accommodates 3).

**Recommended for consistency (non-frontend, optional):** `webapp/server.py:326` startup log; `start.bat:2`
& `:28`; `webapp/__init__.py:2` docstring — swap "HASE assistant" → "MDC assistant".

**Leave alone:** `static/impact.html` placeholders like `mc-hk-hase-ingress-api` / `系统，例如 hase` — those
are **repo-name examples** (repos really are named `mc-hk-hase-*`), not branding. `static/arch_nodes.json`
`HASE HARO` is a real vendor/gateway name — not branding. Don't touch either.
