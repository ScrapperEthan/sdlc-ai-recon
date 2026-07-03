# HASE AI-SDLC Assistant — Project State

Living status doc: **where we are across the full SDLC lifecycle**, updated as we go.
Pairs with `BACKLOG.md` (what to build next) and `docs/specs/*.md` (build-ready specs).

**Last updated:** 2026-07-03

> Legend: 🟢 live / done · 🟡 in progress (beachhead) · ⚪ not started · 🔵 TBD / optional
> Rule: keep it honest — "compiling-shaped" ≠ "builds"; don't mark 🟢 until it actually runs.

---

## Where we are — SDLC lifecycle

| Stage | Status | Reached | What we have | Next |
|---|---|---|---|---|
| **Requirements analysis** | ⚪ | — | — | (later) natural-language ask → structured change spec |
| **Architecture design / impact** | 🟢 foundation | 2026-07-01 | Cross-repo Q&A + impact / dependency / message-routing analysis over the mirror — the retrieval "moat" | Turn understanding into a design proposal for a specific change |
| **Code generation** | 🟡 beachhead | 2026-07-02 | Scaffolding: one command → a convention-faithful new service (parent/starter inherited, SHP/sonar/api layout, package auto-derived, inherited governance values sanitized to `<REVIEW>`) | From skeleton → a real code change **in context** of an existing service |
| **Run tests** | 🟡 beachhead | 2026-07-03 | Internal box has the toolchain (Zulu JDK 21 + Maven 3.9.6). Vertical-slice Step 0 green: an *unmodified* `mc-hk-hase-ingress-api` compiled + tested green in `scratch/probe/`. First real change also passed `mvn test` when run manually | Re-run the full `change.add_endpoint` command post-fix so the tool emits `BUILD_RESULT.md` (PASS) itself |
| **Build** | 🟡 beachhead | 2026-07-03 | Same toolchain; unmodified service compiles in `scratch/`. `change/build.py` now resolves `mvn`→`mvn.cmd` on Windows and never crashes the run on a launch failure | Confirm end-to-end artifact emission on the box |
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

Status: read 🟢 (retrieval) · generate 🟡 (`scaffold/` new modules + `change/` edits) ·
diff 🟢 · compile+test 🟡 (toolchain landed 2026-07-03; Step 0 green, one Windows build-runner
bug fixed, full end-to-end re-run pending) · **write-spec / design ⚪ not built —
the pipeline's front-end is the next big gap.** "Read" comes first because the spec must
be grounded in real code; serve it via the narrow-first router (a domain sub-agent only
later, if needed).

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
- **2026-07-03** — **Vertical slice Step 0 PASSED on the internal box** (toolchain landed: Zulu JDK 21 + Maven 3.9.6). An *unmodified* `mc-hk-hase-ingress-api` copied to `scratch/probe/` compiled and tested green (`COMPILE_EXIT=0`, `TEST_EXIT=0`) — building a HASE service outside its repo is feasible. First real `change.add_endpoint` run generated a correct change (`@GetMapping("/status")` inserted into `IngressResource.java`, `mvn.cmd -q test` passed when run manually, `mirror/` untouched — 3659 files hash-identical) but the tool crashed with `[WinError 2]` before emitting the review artifacts. Root cause + fix: on Windows Maven is `mvn.cmd`; `change/build.py` now resolves `mvn`→`mvn.cmd` via `shutil.which` and records a launch failure as a build failure instead of crashing (so `CHANGE_DIFF.md`/`BUILD_RESULT.md` are always emitted). 8 unit tests pass. **Next: re-run the full command on the box to confirm the tool emits `BUILD_RESULT.md` (PASS) itself → then the generate→verify→diff loop is closed end-to-end.**

---

## How to update this file

When a stage changes status: update its row (Status / Reached / What we have / Next)
**and** append a dated line to the Milestone log. Record the date a stage is first
reached so we can see the pace over time. Keep the honesty rule at the top.
