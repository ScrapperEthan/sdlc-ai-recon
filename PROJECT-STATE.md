# HASE AI-SDLC Assistant — Project State

Living status doc: **where we are across the full SDLC lifecycle**, updated as we go.
Pairs with `BACKLOG.md` (what to build next) and `docs/specs/*.md` (build-ready specs).

**Last updated:** 2026-08-05

> ⚠️ **Honesty note about this file (2026-08-05).** The milestone log below stops at 2026-07-21
> while the repo is at 2026-08-05 — RUNBOOK-46 through RUNBOOK-75 (MDC grouping, the AIOps/MCP
> incident chain, the answer-quality evals, the read-only DB layer, and today's owner answers) are
> **not** recorded here. The stage table above is therefore also behind. Bringing this file back in
> line is tracked work, not a formality: it is the document used to explain the project upward, and
> quoting it today would understate the estate by roughly two weeks of shipped capability.

> Legend: 🟢 live / done · 🟡 in progress (beachhead) · ⚪ not started · 🔵 TBD / optional
> Rule: keep it honest — "compiling-shaped" ≠ "builds"; don't mark 🟢 until it actually runs.

---

## Where we are — SDLC lifecycle

| Stage | Status | Reached | What we have | Next |
|---|---|---|---|---|
| **Requirements analysis** | 🟡 beachhead | 2026-07-03 | Plain-language ask → structured `ChangeRequest` → **retrieval-grounded target** (repo + controller) with a citation-backed rationale; refuses to guess when ambiguous. Verified on the real mirror (`change.from_intent`). Parser is rule-based (swappable for an LLM) | Real LLM parser; free-form intent; widen beyond the add-endpoint template |
| **Architecture design / impact** | 🟢 foundation | 2026-07-01 | Cross-repo Q&A + impact / dependency / message-routing analysis over the mirror — the retrieval "moat" | Turn understanding into a design proposal for a specific change |
| **Code generation** | 🟡 beachhead | 2026-07-03 | Scaffolding (new service) **+ in-context change to an existing service, now driven by intent+retrieval:** `from_intent` locates the target and `add_endpoint` applies a templated change, verified green on `mc-hk-hase-ingress-api`. Change *content* is still templated (add-endpoint) | Widen change kinds (message listener / DAO / config — REPOMAP already indexes these) beyond the endpoint template |
| **Run tests** | 🟢 thin slice | 2026-07-03 | **Proven end-to-end on a real service:** the tool runs `mvn test` on the generated change in `scratch/` and emits `BUILD_RESULT.md` (PASS, exit 0) itself — verified on `mc-hk-hase-ingress-api` (real Maven 3.9.6, Zulu JDK 21) | Broaden beyond a single templated GET endpoint (message listener / DAO / config), and drive the change from a real ask not a hardcoded template |
| **Build** | 🟢 thin slice | 2026-07-03 | Same run: real service compiles + tests green in `scratch/`; `change/build.py` resolves `mvn`→`mvn.cmd` on Windows and records launch failures instead of crashing | Same broadening; later `mvn package` / multi-module |
| **Deploy** | 🔵 TBD | — | — | Stays human-gated. Possibly an **MCP server / skills** so the internal copilot / opencode **assists** a human through deploy (never autonomous) — see "Deploy" below |

---

## The moat (cross-cutting, underneath every stage)

The durable asset is the **retrieval/context layer** over the ~390-repo estate
(`retriever/`, the dependency graph, the message map, the DB snapshot, CodeGraph).
Every stage above is only as good as what the assistant can understand about the
system — so this is where we keep investing. Model + UI are swappable; the moat is not.

## Roadmap mapping (our 3 steps ↔ the lifecycle)

- **Step 1 — cross-repo Q&A** → powers *Requirements-understanding* + *Architecture design*. 🟢
- **Step 2 — new-module scaffolding** → *Code generation*, first slice (skeleton). 🟡
- **Step 3 — batch maintenance at scale** → *Code generation + Test + Build* across many repos (CVE / upgrades). ⚪

---

## Target architecture & future task flow

**How the assistant works (mechanism).** The model does not memorize the code — it's a
tool-using loop: a question triggers retrieval tools (`search_code`/`impact`/`trace`/
`call_graph`…) that read pre-built indexes (code mirror, dependency graph, message map,
CodeGraph), and it answers with `repo/path:line` citations. The indexes + tools are the
durable asset (the moat); the model is swappable.

**Scaling 15 → 390 repos — keep two things separate:**
- **Indexing is FORCED to partition.** CodeGraph can't hold 390 repos in one graph
  (~150 MB / 15 repos), so split the estate into per-domain bundles (public/shared, SMS,
  PN, WhatsApp…), each with its own index — `group.py` bundles + `index/REPOMAP.md` so the
  agent can "narrow first". **Invest here; this is the moat and the key 15→390 step.**
- **Agent topology is a CHOICE.** Start with **ONE agent + a narrow-first router** (pick
  the relevant domain(s), then query only those bundles). **Not** a multi-agent orchestra:
  orchestration (routing, stitching answers, latency, cost, debugging) is fragile, and
  HASE is event-driven / cross-domain by nature (flows span domains) — so a "domain" must
  be a **storage partition, not a wall**. Multi-agent (orchestrator + domain specialists)
  is a valid *later* evolution when a domain gets huge or you want parallelism/isolation —
  it's the swappable brain, so defer it.

**Future task pipeline (one task, end-to-end):**

`read code → write spec.md → design → generate code → compile+test → diff → human review`

Status: read 🟢 (retrieval) · **locate/target 🟡 (intent → retrieval-grounded repo+controller
with cited rationale, refuses to guess — verified on the real mirror 2026-07-03)** · generate 🟡
(`scaffold/` new modules + `change/` edits, now intent-driven) · diff 🟢 · compile+test 🟢
(proven end-to-end 2026-07-03: generate → `mvn test` PASS → diff, `mirror/` untouched) ·
**full write-spec / design for non-templated changes ⚪ — the remaining front-end gap.** "Read"
comes first because the spec must be grounded in real code; serve it via the narrow-first
router (a domain sub-agent only later, if needed).

## Current focus / recommended next: a thin **vertical slice** of the loop

Rather than pushing right toward deploy, the highest-value next move is to close a
thin end-to-end slice for **one real task** (e.g. "add an endpoint / message listener
to an existing service"):

1. **Understand impact** — we have this (Step 1 retrieval). 🟢
2. **Generate a real code change** — extend scaffolding from "skeleton" to actual code
   in the context of an existing service. 🟡→
3. **Compile + test it green** in `scratch/` (`mvn`) — pulls in *Run tests* + *Build*. ⚪→
4. **Produce a diff for human review.**

This single slice is the most credible capability to demonstrate: *the assistant writes
a change that compiles and passes tests, grounded in our own code, without touching prod.*

## Cross-cutting / platform track (parallel to the capability line)

Productionize the assistant itself so a team can actually use it. Currently single-box,
`127.0.0.1`, no auth. Needs: SSO/auth, multi-user, audit logging, index freshness /
re-index. ⚪ — see `BACKLOG.md` #7 (freshness) and #9 (deployment & security).

## Deploy — parked intentionally

In a regulated bank, deploy is heavily governed and **stays human-driven**. We are NOT
targeting autonomous deploy. Option under consideration for later: expose build/deploy
helpers as an **MCP server or skills** so the internal copilot / opencode can **assist**
a human through deploy steps (pre-flight checklists, config diffs, release notes) — the
human still reviews and clicks. Revisit once the vertical slice (generate → test → build)
is solid.

---

## Milestone log (append-only; add a dated line when a stage's status changes)

- **2026-07-01** — Step 1 cross-repo Q&A **live end-to-end** on internal GPT-5.5:
  retrieval tools (`impact`/`consumers`/`producers`/`trace`/`search_code`/`read_file`/
  `call_graph`/`unified_impact`/`citations`), real streaming, citation pills, usage,
  JSON sessions. Pilot = 15-repo ingress→messaging→tracking flow.
- **2026-07-02** — Step 2 scaffolding pilot (spec-driven, delivered via Codex):
  - **P1** — generated `pom.xml` inherits the real `mc-hk-hase-api-parent` + `mc-hk-hase-api-starter`, coordinates derived from the mirror (`docs/specs/scaffolding.md`). Verified on real mirror.
  - **P2** — single thin `*-api` repo made structurally faithful to a real HASE repo: package auto-derived (`com.hsbc.hase.digital.api.<name>`), SHP/sonar platform files, full source layout, starter-only; `*-core` split & `--type job` deferred (`docs/specs/scaffolding-phase2.md`). Verified on real mirror.
  - **Vertical slice — Phase 1 started** (`docs/specs/vertical-slice.md`, `change/`): tool copies an existing service to `scratch/`, adds a GET endpoint in the house style, generates a test, and emits `CHANGE_DIFF.md`. Build is mock-injectable + a `--skip-build` flag; **real `mvn` compile/test deferred — the box has no Java/Maven toolchain yet (Step 0 probe failed; being requested from IT).** 5 tests pass. This begins the *Run tests / Build* stages once the toolchain lands.
  - **P2.1** — copied platform/API files sanitized: inherited governance/account/branch/URL/email values blanked to `<REVIEW>` and listed in `REVIEW_DIFF.md` (`docs/specs/scaffolding-p2-sanitize.md`). `api.meta` (a per-service JSON descriptor) uses aggressive blanking — every string value blanked except the identity keys `assetName`/`contractFileName` (rewritten to the new name); config flags/structure kept. **Verified on the real mirror: no real account/org/business/branch/URL values remain. → Step 2 scaffolding pilot COMPLETE.**
- **2026-07-03** — **Vertical slice Step 0 PASSED on the internal box** (toolchain landed: Zulu JDK 21 + Maven 3.9.6). An *unmodified* `mc-hk-hase-ingress-api` copied to `scratch/probe/` compiled and tested green (`COMPILE_EXIT=0`, `TEST_EXIT=0`) — building a HASE service outside its repo is feasible. First real `change.add_endpoint` run generated a correct change (`@GetMapping("/status")` inserted into `IngressResource.java`, `mvn.cmd -q test` passed when run manually, `mirror/` untouched — 3659 files hash-identical) but the tool crashed with `[WinError 2]` before emitting the review artifacts. Root cause + fix: on Windows Maven is `mvn.cmd`; `change/build.py` now resolves `mvn`→`mvn.cmd` via `shutil.which` and records a launch failure as a build failure instead of crashing (so `CHANGE_DIFF.md`/`BUILD_RESULT.md` are always emitted). 8 unit tests pass.
- **2026-07-03 (Phase 2 VERIFIED on real mirror)** — `change.from_intent` (intent → retrieval-grounded target → templated change → verify → diff) ran green end-to-end on the real box via RUNBOOK 4: unit tests 19 OK; `index/REPOMAP.md` regenerated; `--explain-only` resolved *"add a /status endpoint to the ingress service"* → `mc-hk-hase-ingress-api` + `IngressResource.java` with a cited rationale; full run → `mvn.CMD -q test` **PASS (exit 0)**, `CHANGE_DIFF.md` clean (2 files), `mirror/` untouched (`MIRROR_HASH_UNCHANGED=True`, 3659 files); a valid-path-but-vague ask (*"add a /status endpoint to the api"*) → **REFUSED**, non-zero, candidates listed, no guess. Note: a no-path ask is stopped earlier by the parser, not the resolver — two distinct refusal paths (RUNBOOK 4 Step 4 fixed to reflect this). **The NL-intent → locate front-end is now a working, verified beachhead. Next: widen change kinds (message listener / DAO / config) and/or a real LLM parser.**
- **2026-07-03 (later)** — **Vertical slice CLOSED end-to-end.** After the box pulled the fix, `python -m change.add_endpoint mc-hk-hase-ingress-api --path /status --out-dir scratch` ran to completion: the tool itself emitted `CHANGE_DIFF.md` (2 files — `IngressResource.java` +4 lines, new `IngressResourceStatusTest.java`) and `BUILD_RESULT.md` (**`mvn.CMD -q test` → PASS, exit 0**); `mirror/` untouched (`MIRROR_HASH_UNCHANGED=True`, 3659 files). The **read → generate real change → compile+test → reviewable diff** loop now works on a real HASE service, prod untouched. Cosmetic caveat: an existing test prints a stack trace into the output tail (exit code still 0). **This is the thin-slice proof; next is widening the change kinds and driving the change from a real ask (NL intent + retrieval) rather than a hardcoded `--path` template — the pipeline front-end.**
- **2026-07-07 — A sibling team's AI-SDLC skill-hub found; scaling + skill-reuse plan set.** Another HASE team already runs a full requirements→design→code→review→SIT-test workflow as GitHub Copilot **Agents + Skills** (3 agents, 14 skills; `agents/*.md` + `skills/<name>/SKILL.md`; **no MCP**). Internal Codex evaluated their real clone (RUNBOOK 5 → local `PEER-SKILL-HUB-EVAL-REPORT.md`): most front-end/review skills are **portable pure-prompt markdown** (`prd-writing`, `tdd-writing`, `api-sit-case-*`, `api-test-result-reviewer`); runtime-bound ones (`jira-data-search`, `rag-data-search`, `pib-api-test-executor`) hardcode internal endpoints; coding skills (`domain-papi-*`) are PIB-specific. **They work single-repo → no cross-repo blast-radius, which is exactly our moat's fill.** Decisions (user): (1) **scale our index 15→~390 repos first** (CodeGraph + retrieval, domain-partitioned); (2) **fork their portable front-end skills into a separate repo** and adapt ourselves — **do not depend on their team** (they provide only the repo as-is); (3) dev teams run skills in **GitHub Copilot Agent mode**, so integrate our moat as a **`cross-repo-impact` skill that curls a centrally-hosted retrieval endpoint** (mirroring their `rag-data-search`), invoked by the Plan/Review agents; (4) also hand dev teams our existing cross-repo **Q&A app**. Analysis: `docs/PEER-TEAM-ANALYSIS-zh.md`, `docs/PEER-MEETING-PREP-zh.md`.
- **2026-07-07 — Domain partition sized + planned (RUNBOOK 6 verified on the real 385-repo dep graph).** Key facts: **dependency graph is already full-estate (385 repos)** → cross-repo *dependency* impact works at ~390 scale **today** (it's POM-derived, needs no mirror); the **mirror + CodeGraph + message-map are still pilot-only (15 repos)** and scale together (message-map is code-derived). Partition strategy validated: name-token bundles, ≤~60 repos each → ~0.23–0.62 GiB CodeGraph DBs (feasible); `svc`(140)/`ssvc`(63) too big → split by sub-token; hub overlap is expected+benign; `tracking` is a cross-cut. Plan + rationale (professional + plain-language, for explaining to others): `docs/DOMAIN-PARTITION-PLAN-zh.md`; next Codex task = `make_bundles.py` → reviewable `index/bundles.json` computed from the full dep graph (runs now, no mirror needed). Sequencing decision: **build the retrieval service (Track 2/3) now** — its headline `/impact`+`/hubs` value rides the already-full dep graph and doesn't wait for mirror/CodeGraph scaling.
- **2026-07-08 — Track 2/3 implementation landed in-repo; box-side generation/verification handed off.** External Codex added `retrieval_service.py` (stdlib HTTP wrapper over `retriever/`, separate process from the Q&A app), `make_bundles.py` (reviewable domain partition planner that emits `index/bundles.json` from `recon_out/internal_edges.csv`), stdlib unit tests for both, and a build-ready `docs/skills/cross-repo-impact/SKILL.md` draft for the separate skills repo. Because this checkout does **not** include the real `mirror/`, `index/`, or `recon_out/` estate data, the remaining work is intentionally on the internal box: run `make_bundles.py` against the full dep graph to generate the real `index/bundles.json`, then start `retrieval_service.py` beside the unchanged Q&A app and verify endpoint parity on real inputs.
- **2026-07-08 — Partition LOCKED + service verified (RUNBOOK 7 on the box); impact-report + tags specced.** `make_bundles.py` on the real full dep graph → **31 bundles, 390/390 coverage**, only `tracking` flagged (≈620 MiB) → **accepted as-is, partition locked**. `retrieval_service.py` verified running beside the unchanged Q&A app with endpoint parity to `cli.py` (bad repo→404, POST→405). Next builds specced: `docs/specs/impact-report-and-repo-tags.md` (EXTERNAL Codex) — **`impact_report.py`** (the flagship *use-case/repo/topic → upstream + downstream + async + channel, cited* report = incident-response + change-notification) and **`make_repo_tags.py`/glossary** (narrow-first retrieval); verified on the box via `RUNBOOK-8`. Leadership approval doc to start per-bundle CodeGraph builds: `docs/PLAN-zh.md`.
- **2026-07-08 — Impact report + tags/glossary built, reviewed, merged (31 tests) + verified on the box (RUNBOOK 8).** `impact_report.py` (repo/topic/use-case → upstream + downstream + async + channel, cited), `make_repo_tags.py`, `retriever/glossary.py`+`repo_tags.py`, `/impact-report`+`/repos` routes — all additive, Q&A app untouched. **Box verification green:** repo/topic reports cite real source (e.g. `WhatsappListener.java:35`); `impact_report` downstream count == `cli.py impact` (53==53); `/impact-report` endpoint matches CLI. **Honest data-coverage caveat:** use-case→channel is limited by the pilot message-map + dev/SCT routing snapshot (some use-cases resolve a route but channel unknown, some vice-versa) — fills in with the full mirror + fresh routing table + channel overrides. Real artifacts generated box-local (gitignored): `glossary.json` (259 tokens, but ~209 need descriptions), `repo_tags.json` (390 repos; system 95%, mode 88%, **channel only 35%** — the gap the boss's per-repo channel sheet fills via `repo_tags.override.json`). **Next:** demo UI (`docs/specs/impact-demo-ui.md`, front-end over the existing service) to present the capability; channel-override from the boss's sheet; leadership approval → per-bundle CodeGraph builds.
- **2026-07-09 — MDC delivery architecture mapped; outage-impact capability built + reviewed (34 tests).** The messaging (MDC) notification pipeline decoded (memory `mdc-messaging-architecture`): `Ingress→Decision→per-channel Topics→Delivery Jobs→Outbound APIs→vendors→Client`, where **channel+vendor are encoded in repo names** (derivable from the full dep graph, no mirror) and a **use-case = a routing id → topic** (`tbl_event_router_usecase_topic.snapshot.csv`; channel is a token in the topic name). Built (from `docs/specs/outage-impact.md`, reviewed + Claude fixed a cosmetic markdown bug): `make_delivery_topology.py` (structural parse, no hardcoded vendors), topic→use-case reverse index in `messages.py`, `outage_report.py` + `/outage-impact` endpoint + a **「故障影响」demo tab** — input a failed channel/vendor/delivery-job → affected **use-cases + repos**, cited. **Two tiers:** channel-level = solid today; vendor-level = heuristic (token-match) until the full message map (topic→delivery-job edges) lands — which is the concrete payoff of the full-mirror/CodeGraph resource ask. Box verification: `RUNBOOK-10`. This is the messaging team's 25-min outage-response use case.
- **2026-07-20 — Use Case master data + upstream `source_system` (Tier 0) built (from `docs/specs/use-case-master-and-upstream.md`; Sonnet 5 built directly, not external Codex).** `tbl_use_case.csv` (1,175 × 63, dev/SCT) is a **second snapshot on the same `use_case_id`** as the routing snapshot, joined via new `retriever/usecase_master.py` (read-only, stdlib, missing-file-safe). Wired additively into every existing surface: `impact_report.build_usecase_report` now enriches `target.business`/`target.governance`/`consent_preflight` (byte-identical fallback when the master snapshot is absent — regression-tested); `outage_report.affected_use_cases` gains name/source_system/owner the same way. **New entry point:** `source-system:<name>` (parse_target, CLI, `webapp/tools.py` `source_system_impact`+`list_source_systems`, `mcp_server.py`, `/source-system-impact`+`/source-systems` on `retrieval_service.py`) — aggregates a source system's Use Cases, **splitting routed (traced channel) from catalog-only (business registration, no traced route)** so the blast radius is never padded with the untraceable ~72% (850/1175 in the field spec's estimate). `owners` (distinct `created_by`/`modified_by`) directly serves the "who do I notify on change" ask. Arch map gets a hidden-by-default **business-upstream gutter** (`static/arch_nodes.json` `business_sources`, `retriever/arch_focus.py` `kind=source-system|use-case`, `static/arch.html` renders it only on that focus) — verified live in-browser (banner + gutter node render/clear correctly, vendor/channel highlight regression-checked). `quality_report()` wired into `refresh.py` as an additive step (`index/reports/USECASE_QUALITY.{md,json}`, gitignored) — join coverage, missing `source_system`, staleness, illegal `business_category` enum codes (data-contract drift, e.g. `33`), junk `work_stream_name`. 24 new tests, 195/195 total pass, no regressions. **Honesty carried through:** consent/opt-in flags are pre-send policy checks, NOT the channel list — the real channel chain + bounce-back fallback wait on `tbl_use_case_channel_rule` (Tier 1, not yet available). **Box verification (real snapshot data, real counts) is a follow-up RUNBOOK, not yet run** — this entry covers the code build + fixture-based tests only.

- **2026-07-21 — UAT Use Case catalog Round A: P0 correctness fixes + 3-table ingest (from `docs/specs/use-case-uat-catalog.md`; Sonnet 5 built directly).** The UAT export (`tbl_use_case` 2,810×63, `tbl_use_case_channel_rule` 6,217×42, `tbl_use_case_ext` 2,660×36) proved the just-shipped Tier 0 wrong on real data: it labelled UAT rows `dev/SCT`, bound `status` to `unknown_bounce_back_status` (first-column-wins), and computed routed/coverage off the stale dev/SCT route snapshot. **`retriever/usecase_master.py` is now a thin facade** over new **`retriever/usecase_catalog.py`**, which owns a manifest-driven `Dataset` abstraction (`index/usecase-snapshots/active/manifest.json`, env var `SDLC_USECASE_DATASET`; legacy single-CSV back-compat preserved, `environment` reported as `unknown` not `dev/SCT`). **Eight defects fixed:** (1) real `environment` from the manifest; (2) **cross-environment route guard** — coverage never reaches across to a differently-labelled route snapshot, route dimension reports `unavailable (no same-environment route snapshot)` instead of a silent wrong join; (3) `resolve_column()` replaces "first column containing the needle" with exact→alias→unique-fuzzy→ambiguity, so `status` wins over `unknown_bounce_back_status`; (4) consent binding accepts UAT's singular `marketing_insight_push_optin_flag`; (5) `canonicalize_source_system()` folds case/format variants (eAlert/e-Alert/…) automatically, alias file only needed for genuinely different spellings (PEGA/PEGA_HK), `MDC Test` never auto-merged into `MDC`; (6) `status=N` (113 UAT rows) excluded by default (`include_inactive` to add back); (7) **layered owners** — `business_owners` (Ext real contacts) > `cost_governance` (sign-off) > `config_maintainers` (created_by/modified_by, now correctly labelled as maintenance-only); (8) endpoint→repo resolver (`resolve_endpoint`) replaces the 5 static arch upstreams with UAT's 154 raw endpoints + `Ext.endpoint` chains, skipping version tokens, data/confidence only (no arch rendering yet). **New coverage funnel** (`configured`/`expression_ready`/`entrypoint_traceable`/`catalog_only`) replaces the old routed-vs-catalog-only split everywhere: `impact_report.build_source_system_report`, `build_usecase_report` (adds `channels_declared` from the channel-rule FACT + `endpoint_repos`), `outage_report.affected_use_cases` (owner now prefers the real business owner), `webapp/tools.py`/`mcp_server.py`/`retrieval_service.py` (`source_system_impact` gains `include_inactive`/`offset`/`limit`, defaults to top-50 so MDC-scale (~880) responses never overflow context; `list_source_systems` returns canonicalized `raw_variants`). `quality_report()` extended with `column_bindings` (bound + ambiguous, never silently picked), `active_inactive`, the funnel, `route_dimension`. Channels are Round A **fact-only** from `channel_rule.channel` — `rule_text` stored raw, NOT parsed (AST is Round B). 42 new tests (28 module unit tests + 8 real-width 63/42/36 header-contract tests + 3 MDC-880 pagination tests, plus updated fixtures) — 236/236 total pass, no regressions. **Box verification against the real UAT CSVs is `RUNBOOK-45` (internal Codex, not yet run)** — this entry covers the code build + fixture-based tests only. Round B (rule_text AST, Catalog UI, dynamic arch) explicitly deferred pending box verification + owner answers (RUNBOOK-45 Part B).

When a stage changes status: update its row (Status / Reached / What we have / Next)
**and** append a dated line to the Milestone log. Record the date a stage is first
reached so we can see the pace over time. Keep the honesty rule at the top.

- **2026-08-05 — Three owner answers landed and were implemented at three different strengths; the glossary stopped faking decodings.** The DB connection work is parked (waiting on the DB owner), so this round is everything that does not depend on it.
  - **`business_category` — the data dictionary defines 0–7 and stops there.** This makes the 33/37 finding *stronger*, not weaker: three sources now disagree (dictionary 0–7 / `BusinessCategoryEnum.java` also 8, 10–21, 32, 34, 35 / real UAT rows also **33** and **37**, all `status=Y`). Flattening those into one "known enum" is exactly what produced the earlier wrong claim that 33/37 were unregistered *new* categories — they are registered nowhere. `resolve_business_category()` now returns a `source` (`data_dictionary` / `code_enum` / `undefined`) and only `undefined` is a defect; codes defined in code but absent from the dictionary excerpt are reported, never alarmed. Enums moved out of code into `config/business_enums.json`.
  - **`>` right-hand side = 左边失败了才发.** Closes the one follow-up 2026-07-27 left open. `stage_transition` = `fallback_on_failure`, validated fail-closed like the operator meanings (an unrecognised value reads as unconfirmed, never passed through). The **second** reading matters more than the outage one and is now explicit as `stages_are_live`: while the earlier stage is healthy the later channels are **not sending**, so an impact answer must not count every channel in a `>` expression as live traffic.
  - **Blank `vendor` ↔ `traffic_percentage`, deliberately implemented as a CHECK not a rule.** The owner said blank vendor is "**基本上**" a 0% route. `traffic_percentage = 0` → does not send is definite and the engine acts on it (new `retriever/traffic.py`, tri-state `sends` where blank is **unknown, not zero**); the vendor correlation is verified per row (`vendor_blank_explained`), because a blank carrier on a route that *is* carrying traffic is the residue worth seeing and a rule would have deleted it from view. Idle channels now surface at the `exit_path` top level so blast radius stops over-counting.
  - **Glossary: unfilled entries no longer render as decodings.** A live screenshot showed `mc-hk-hase-api-common (mc=TBC ? ???????, …)` — the box's hand-authored `index/glossary.json` had placeholder values and `expand()` printed whatever it was given, indistinguishable from a real decoding (the same shape as reporting a log tail as a keyword hit). Placeholders and `???` runs are now dropped; a non-UTF-8 file degrades instead of raising `UnicodeDecodeError` straight through every impact report; and `refresh.py` emits `index/reports/GLOSSARY_COVERAGE.md`, a fill list ranked by how many real repo names each token appears in — the box had ~209 unfilled tokens and no way to know which mattered.
  - **Bonus from the same data dictionary: `send_mode` (1 same time / 2 by priority / 3 single channel) is an INDEPENDENT third registration of the semantics `rule_text` and `channel_rule.priority` also describe.** Bound optimistically (we have seen the dictionary, not the export header) and wired as a free cross-check on `rule_text` — never an override, since rule_text stays authoritative. Disagreements are reported like the existing rule_text-vs-priority ones.
  - 1425 tests pass. Box verification is **RUNBOOK-75** (not yet run): it measures how often "基本上" actually holds, whether `send_mode` exists in the real export, how many use cases change blast-radius shape under fallback semantics, and walks the glossary fill.
