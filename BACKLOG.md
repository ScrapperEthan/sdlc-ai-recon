# Backlog — complex work worth doing (hand to Codex)

Each item is self-contained: a fresh Codex with no chat history should be able to
pick one up. Do them one at a time, in a branch, and keep changes scoped to the
files named. Ask the maintainer before starting anything marked **needs a decision**.

> **Last reconciled against the repo: 2026-08-06.** The previous version of this file
> listed twelve items of which **eight had already shipped** — a fresh Codex following it
> would have rebuilt the eval harness, the citation guard and the refresh pipeline from
> scratch. If you are reading this more than a few weeks later, check **"Already built"**
> below and spot-check the repo before starting anything.

## Project context (read first)

This repo (`sdlc-ai-recon`) is the retrieval + assistant stack for a ~460-repo
Java/Spring estate (org `hase-mc`) that forms ONE product. Five lines of work live here:

- **Recon** (`harvest_poms.py`, `recon_maven_graph.py`, `refresh.py`, `RUNBOOK*.md`): builds
  the dependency graph `recon_out/internal_edges.csv`, the async message map
  `index/message_edges.csv`, per-repo tags, and a read-only code mirror `mirror/`.
- **Retrieval layer** (`retriever/`, `cli.py`, `mcp_server.py`, `retrieval_service.py`):
  read-only tools — deps (`impact`, `unified_impact`, `blast_radius`), messages
  (`consumers`/`producers`/`trace`), use-case catalog (`usecase_catalog`, `usecase_router`,
  `rule_text`), delivery chain to the vendor exit (`delivery_chain`), code
  (`search_code`/`read_file`), `call_graph` shells to CodeGraph.
- **Web app** (`webapp/`): browser chat → `server.py` → `agent.py` tool loop → `llm.py`
  facade → `llm_providers/*`. Behaviour/citation rules live in `prompts/qa-system-prompt.md`.
  Sessions persist in `webapp_data/`.
- **Incident / AIOps line** (`webapp/incident_*.py`, `webapp/mcp_client.py`,
  `webapp/mcp_registry.py`, `retriever/incident.py`): alert → which logs to look at →
  cited root cause → business blast radius, executed over **colleague-owned MCP servers**
  (LogDream / CloudWatch / Portal). The seam: **we own the query plan, they execute it.**
- **Read-only DB layer** (`webapp/db_readonly.py`, `webapp/db_registry.py`,
  `config/db_queries.json`): built and unit-tested, **not yet run against a real DB**
  (blocked on UAT RDS Proxy read-role auth).

## Guardrails (do NOT violate)

- **Read-only on production.** Never modify/clone-write the `hase-mc` repos. Only read
  `mirror/`, `recon_out/`, `index/`. Generated files stay in those folders.
- **Air-gapped / install-restricted.** Prefer the standard library. `pip`/`npm` installs may
  be blocked; if an item needs a package, make it optional with a stdlib fallback and say so.
- **Bank security.** No secrets in code, no data leaving the network, no autonomous DB access.
- **The intranet Codex CANNOT push to this repo (confirmed 2026-07-30).** Anything the box
  needs to edit must be a **committed file under `config/`** that the box edits locally, with
  a gitignored `*.local.json` override that automatically wins. Never commit a *guessed* value
  into those files — a committed guess is the defect the whole seam exists to prevent.
- **Never assert anything about their environment.** Five verification rounds each caught the
  same shape, sharpening in sequence: their **names** → their **response shapes** → their
  **value formats**. Every fix moved something out of code and into `config/`. Keep the
  abstract side; let them own every real name, shape and format.
- **Fail closed.** If a precondition cannot be verified, refuse and emit nothing — do not
  "proceed anyway". A refused plan must not still send the request (RUNBOOK-61).
- **Placeholders are not answers.** `TBC` / `???` / `unknown` / blank must be DROPPED, never
  rendered as a meaning and never counted as `known=True`. Reuse
  `retriever/glossary.is_unfilled()` rather than writing a second gate.
- **One name, one binding** (`webapp/` layering). `from x import CONST` for constants only;
  patch the owner module, never the importer — otherwise tests go green while the real
  function runs. `incident_parse` must not import `retriever`.
- **Merge-conflict rule.** `webapp/llm.py` is a stable facade — don't edit it for provider
  work; provider/protocol changes go in `webapp/llm_providers/*`.
- **Contract.** The model call returns an OpenAI chat-style message
  (`{"role","content","tool_calls"?}`); `agent.answer()` returns
  `{"answer","tool_trace","usage","citations"}`. Keep these stable.

---

## Already built — DO NOT REBUILD

Verified present in the repo on 2026-08-06. Each was an open backlog item in the previous
version of this file.

| Was item | Now | Where |
| --- | --- | --- |
| Citation verification guard | **shipped** | `retriever/citations.py`, called in `agent.py` (4 call sites); `agent.answer()` returns `citations` |
| Eval harness | **shipped** | `evals/run.py` + `evals/cases.jsonl` (35 cases), `config/eval_repos.json`; baseline run = RUNBOOK-66 |
| Clickable citations → source viewer | **shipped** | `GET /api/source` in `server.py`, viewer in `static/app.js` |
| Message map / producer coverage | **closed 2026-07-20** | `producer_extract.py`, `message_map_enrich.py`; RUNBOOK-42 |
| Scale the index beyond the 15-repo pilot | **done** | 31 bundles, 460-repo universe; `make_bundles.py`, CodeGraph 31/31 |
| `index/REPOMAP.md` | **shipped** | `make_repomap.py` |
| Freshness / re-index pipeline | **shipped** | `refresh.py` (+ `--repos-file`), `index/last_indexed.json` |
| Real streaming | **shipped** | SSE path in `server.py`, live tool chips in `static/app.js` |
| Usage dashboard | **shipped** | `GET /api/usage` (`server.py:336`), `webapp/llm_usage.py` |
| Scaffolding generator | **shipped** | `scaffold/`, `change/` — verified end-to-end on the box 2026-07-03 |
| Unified impact (sync + async + deps) | **shipped** | `retriever/unified_impact.py`, `retriever/blast_radius.py` |
| Hub blast-radius tiering | **shipped 2026-08-05** | `retriever/blast_radius.py` — past a hub threshold the answer leads with direct dependents + channel spread + critical repos |

---

## Open items

Order: data leverage (1, 2) → capability (3) → safety net (4) → operations (5, 6).

### 1. Glossary candidates from the MDC sheet's second tab — [P1] [Effort: S]

**Goal:** stop hand-typing an abbreviation dictionary the intranet has already mostly written.

**Why:** `index/glossary.json` is hand-authored on the box and gitignored — `retriever/glossary.py`
says so in its own docstring: *nothing in this repo generates it*. Measured 2026-08-05: token
slots 78.6% decoded, but only **42 of 460 repo names have every token decoded**. That is the
number a reader actually experiences, and filling it by hand means ~259 tokens of somebody's day,
which is why it has stayed unfilled.

**New information (maintainer, 2026-08-06):** `MDC_Repo_List_Analysis_v0.3.xlsx` — the workbook
`enrich_repo_tags.py` already reads — has a **second sheet, `Keyword in Repo Name`**, with columns
`Name in Repository Name` / `Description` / `Sample`. It covers most of the common tokens. We are
simply not reading it.

**Where:** `mdc_sheet_schema.json` (the column-map knob), `enrich_repo_tags.py` (already opens
this workbook for `full Repository List`), `retriever/glossary.py` (the consumer).

**Approach:** add a second sheet block to the schema knob — sheet name + column aliases, reusing
the existing case/space/punctuation-insensitive alias matching, and the existing "field's column is
missing → leave empty, never crash" rule. Emit `index/glossary.candidates.json` as
`{token: {meaning, source, row}}`. Then:

- **Merge, never overwrite.** A box-authored `glossary.json` entry always wins over a candidate.
- **Run every value through `glossary.is_unfilled()`** before accepting it. The sheet has blank and
  `TBC` rows; a placeholder that becomes a definition is the exact defect the enum loader closed
  on 2026-08-05, and it is worse here because downstream code branches on "known".
- **Commit no values.** This repo ships the reader; the box owns every meaning. Do not transcribe
  sheet contents into code, fixtures or docs — transcribed values have been wrong before
  (`hk1` vs `hkl`).
- Report the delta: `coverage()` before vs after, and which tokens are still dark.

**Done when:** running the enrich step on the box produces candidates for the tokens in that sheet;
`repos_fully_decoded` moves; a blank/`TBC` row in the sheet does **not** become a definition; and no
sheet value is present in any committed file.

### 2. Real channel coverage: promote the evidence tiers — [P1] [Effort: M]

**Goal:** make "which channels does this change/outage affect" a number somebody can act on.

**Why:** the authoritative `channel` tag comes from `detect_channels()` in `make_repo_tags.py:132`,
which matches `CHANNEL_KEYWORDS` against **the repo name**. A repo whose name doesn't contain
`sms`/`email`/`push`/`whatsapp`/`wechat`/`letter` is untagged, so the channel spread on any impact
answer is a **lower bound over a mostly-untagged set** — and a confident-sounding one.

**The MDC sheet does not close this**, and the code already says so:
`enrich_repo_tags.reconcile()`'s honesty block states the sheet *adds business metadata and
reconciliation evidence*, not fixed-channel ownership coverage. The sheet's `channel_flags`
(`SMS`/`EMAIL`/`PUSH`/…) cover the MDC roster, whose repo names largely already carry the channel;
the several hundred repos with no channel tag are **not in the sheet at all**.

**What already exists to build on:** `make_repo_tags.py` computes `serves_channels` (graph
blast-radius), `msg_channels` (from the message map), `channel_declared` (sheet), plus the
`channel_explained` / `channel_true_dark` split. The tiers are computed; they are just not what the
narrative leads with.

**Where:** `make_repo_tags.py`, `retriever/repo_tags.py`, `retriever/blast_radius.py`,
`impact_report.py`.

**Approach:** surface the tiers with their evidence named rather than collapsing them into one
field — *owns it* (name), *declared* (MDC sheet row N), *serves it* (dependency graph), *carries it*
(message map). Report `channel_true_dark` — the repos **no tier can explain** — as the lower-bound
caveat, instead of the raw untagged count, which today over-states the unknown. Keep
`serves_channels` out of the channel-spread count where 13bf8e0 already excludes it: it is derived
from the same graph relationship being measured, so counting it lets a repo inherit a channel from
the very edge under test and hand it back as independent evidence.

**Done when:** a hub repo's impact answer lists each affected channel with its evidence tier, the
lower-bound caveat quotes `channel_true_dark`, and a repo whose only evidence is `serves_channels`
is never reported as owning that channel.

### 3. Widen the change kinds beyond add-endpoint — [P1] [Effort: L]

**Goal:** cover the changes developers actually make, not one template.

**Why:** `change/` can generate exactly one kind of change — add a GET endpoint
(`change/add_endpoint.py`). This has been the *Code generation* row's stated Next since
**2026-07-03 and has not moved.** Adoption is capped by coverage of everyday work, not by polish:
a developer who tries it twice on work it cannot do stops opening it.

**Where:** `change/` (`intent.py`, `locate.py`, `add_endpoint.py`, `build.py`, `from_intent.py`),
`scaffold/reference.py` for the house style.

**Approach:** add change kinds one at a time, each with the same end-to-end shape the endpoint kind
already proves — intent → retrieval-grounded target → templated edit → `mvn test` → `CHANGE_DIFF.md`,
mirror untouched. Priority order to confirm with the maintainer, suggested: **message listener**
(the estate is event-driven, so this is the most common real change), then **DAO/repository method**,
then **config**. Refuse rather than guess when the target is ambiguous — the existing two refusal
paths (parser-level and resolver-level) are the model.

**Done when:** at least one new change kind runs green end-to-end on the real mirror on the box,
`MIRROR_HASH_UNCHANGED=True`, with an ambiguous ask still refused and candidates listed.

### 4. Cheap eval lanes: record/replay + stub-model tool-trace — [P1] [Effort: M]

**Goal:** know that an answer got worse *at commit time*, not one intranet round later.

**Why:** there are two tiers and a hole between them. ~1478 unit tests check plumbing and cannot
judge an answer. `evals/run.py` judges answers but **costs one real model turn per case** and is
explicitly a before/after gate run on the box. So routine prompt and tool-description edits ship
with no signal at all. And the failure has already bitten in the other direction: the RUNBOOK-66
baseline reported 14/20, but **five of the six reds were the assertions being wrong**, not the model
— nearly steering a fix at the wrong target.

**Where:** `evals/` (new `record.py` / `replay.py`), `evals/run.py` (reuse the checkers), a stub
provider under `webapp/llm_providers/`.

**Approach:** two lanes, both zero-cost, both stdlib.

- **Replay lane.** A box run writes each case's final answer + full tool trace to a fixture. The
  replay lane re-runs *only the assertions* against those fixtures. This catches assertion
  regressions and lets an assertion fix be validated without spending a model turn. Fixtures are
  box-produced, so they are gitignored — ship the recorder and an empty directory, never a
  hand-written "expected answer".
- **Tool-trace lane.** A scripted stub model drives `agent.answer()` so the check is purely
  *did it call the right tool, with the right arguments, and did it fail closed* — no prose
  judgement. This is where the timezone/scope/zero-call rules belong.

**Done when:** both lanes run offline with no API key and no mirror, they are wired into the normal
test command, and a deliberately broken assertion is caught by the replay lane.

### 5. Team deployment & security hardening — [P2] [Effort: M] **needs a decision**

**Why:** it still binds `127.0.0.1` with no auth and no audit trail. Fine for one tester; in a bank,
no audit trail is itself a blocker for a shared service.

**Where:** `webapp/server.py`, config, deployment notes.

**Approach:** split it. Ship the parts that need no governance sign-off first, all behind flags that
**default to today's behaviour**: request/audit logging, a max-concurrency guard
(`ThreadingHTTPServer` is unbounded), basic rate limiting. The parts that DO need a decision —
binding beyond localhost, SSO/reverse proxy, the data-governance story for a shared internal
service — must be confirmed with the maintainer before any of it is exposed.

**Done when:** audit logging + concurrency + rate limiting are on by config with localhost mode
byte-identical to today; the exposure decision is recorded, not assumed.

### 6. Split `webapp/incident_investigator.py` — [P3] [Effort: M]

**Why:** at ~103 KB it is more than twice the next-largest file in the repo. The 2026-08-04 split
(`05780b6`) extracted plan/parse/redaction; the orchestration layer kept everything else. The
"one name, one binding" rule is hardest to hold exactly where the file is biggest, and that rule
exists because a violation makes tests pass while the real function runs.

**Where:** `webapp/incident_investigator.py`.

**Approach:** split along the seams that already exist in the flow (evidence collection / branch
orchestration / packet assembly + egress gate), not by line count. The egress gate especially wants
its own module — it is the last thing between the investigation and what leaves the process, and it
has already needed two fixes (value-wise fingerprinting, alarm-name leakage).

**Done when:** no behaviour change, the test suite is green, and the egress gate is separately
testable.

### Small items — **verify each is still open before starting**

- **Prompt-based tool-calling fallback** in `agent.py` for endpoints without function-calling
  (parse a JSON action out of text). [S]
- **Optional semantic search retriever** using the internal embedding model as a new tool, with the
  existing lexical `search_code` as the no-embedding fallback. [M]
- **Harden `llm_providers/copilot_responses.py`** against the real copilot-api response shape. [S]

---

## Blocked on someone else — not buildable here

Building more does not move these. Listed so nobody spends a round working around them.

| Blocked on | What it gates | Who |
| --- | --- | --- |
| **service ↔ LogDream app-name map** (hot ~20) | incident coverage — the engine is finished and reads the map with no code change; only one app is mapped, so the capability is ~0% usable | intranet, ~half a day |
| UAT RDS Proxy **read-role auth** | live DB queries — the layer is built and unit-tested, never run against a real DB | DB owner |
| A **same-moment** export of the 5 UAT tables | every cross-table count is a cross-time join today | DBA, one ticket |
| **Vendor alias sign-off** (`HTCL` / `HTCL OLD` / `AWS HK SNS` / `AWS SG SNS`) | naming a carrier instead of quoting a raw string; the mapping is already evidenced, it needs one "yes" | business owner |
| `send_mode = 0` (903 rows) | one code-table entry; those rows stay `pending` until it lands | business owner |
| Portal MCP (8094) availability | the largest alert family | peer team |

**The first row is the highest-leverage unblock in the project**: a completed capability sitting at
near-zero usable coverage for want of one table. If a backlog item ever competes with chasing that
map, chase the map.
