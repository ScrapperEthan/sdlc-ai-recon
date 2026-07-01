# Backlog — complex work worth doing (hand to Codex)

Each item is self-contained: a fresh Codex with no chat history should be able to
pick one up. Do them one at a time, in a branch, and keep changes scoped to the
files named. Ask the maintainer before starting anything marked **needs a decision**.

## Project context (read first)

This repo (`sdlc-ai-recon`) is the retrieval + assistant stack for a ~390-repo
Java/Spring system (org `hase-mc`) that forms ONE product. Pieces:

- **Recon** (`harvest_poms.py`, `recon_maven_graph.py`, `RUNBOOK*.md`): builds the
  dependency graph `recon_out/internal_edges.csv`, the async message map
  `index/message_edges.csv`, and a read-only code mirror `mirror/`.
- **Retrieval layer** (`retriever/`, `cli.py`, `mcp_server.py`): read-only tools —
  `impact` (deps), `consumers`/`producers`/`trace` (messages),
  `usecase_route` (uses `index/tbl_event_router_usecase_topic.snapshot.csv`),
  `search_code`/`read_file` (mirror). `call_graph` shells to CodeGraph.
- **Web app** (`webapp/`): browser chat → `server.py` → `agent.py` tool loop →
  `llm.py` facade → `llm_providers/*`. Behaviour/citations rules live in
  `prompts/qa-system-prompt.md`. Sessions persist in `webapp_data/`.

## Guardrails (do NOT violate)

- **Read-only on production.** Never modify/clone-write the `hase-mc` repos. Only
  read `mirror/`, `recon_out/`, `index/`. Generated files stay in those folders.
- **Air-gapped / install-restricted.** Prefer the standard library. `pip`/`npm`
  installs may be blocked; if an item needs a package, make it optional with a
  stdlib fallback and say so.
- **Bank security.** No secrets in code, no data leaving the network, no
  autonomous DB access. A one-time read-only DB export is done by a human.
- **Merge-conflict rule.** `webapp/llm.py` is a stable facade — don't edit it for
  provider work; provider/protocol changes go in `webapp/llm_providers/*`.
- **Contract.** The model call returns an OpenAI chat-style message
  (`{"role","content","tool_calls"?}`); `agent.answer()` returns
  `{"answer","tool_trace","usage"}`. Keep these stable.

## Recommended order

Trust first (1, 2, 3), then coverage (4, 5, 6, 7), then experience/ops (8, 9, 10),
then new capability (11, 12).

---

### 1. Citation verification guard — [P1] [Effort: M]
**Goal:** Prove the assistant isn't fabricating references.
**Why:** The product promise is "every claim cited `repo/path:line`." Verify it.
**Where:** `webapp/agent.py` (post-process the final answer), `retriever/code.py`.
**Approach:** After the model's final answer, extract every `repo/path:line`
citation, check the file exists under `mirror/` and the line is in range. Return
a `citations` list on the result: `[{ref, ok, reason}]`. Optionally append a
one-line "⚠ N citations could not be verified" note. Do NOT rewrite the answer.
**Done when:** `result["citations"]` is populated and a planted fake citation is
flagged `ok=false`; real ones pass.

### 2. Eval harness (regression for answer quality) — [P1] [Effort: M]
**Goal:** Measure answer quality objectively as prompts/tools/models change.
**Why:** Today evaluation is eyeballing `index/qa-eval.md`. Make it repeatable.
**Where:** new `evals/` (dataset `evals/cases.jsonl`, runner `evals/run.py`).
**Approach:** Each case = `{question, must_mention_repos[], must_cite_globs[],
must_flag_partial?}`. Runner calls `agent.answer` (or the HTTP API), scores:
did it call the expected tools, mention the expected repos, cite matching files,
correctly mark partial. Emit a score table + diff vs last run. Stdlib only.
Seed with the 5 pilot questions already in `RUNBOOK-2/3`.
**Done when:** `python -m evals.run` prints per-case pass/fail + an aggregate score.

### 3. Clickable citations → source viewer — [P1] [Effort: M]
**Goal:** Click a `repo/path:line` pill and see the actual cited source.
**Why:** Turns "trust me" into "look yourself"; huge for reviewer confidence.
**Where:** `webapp/server.py` (new read-only `GET /api/source?path=&line=`),
`webapp/static/index.html` (the `.cite` pills + a modal/side panel).
**Approach:** Endpoint calls `retriever.code.read_file` with a small window
around the line; frontend opens a panel showing line-numbered source, target
line highlighted. Path must be validated to stay inside `mirror/` (no `..`).
**Done when:** clicking a citation shows the correct file window; path traversal
is rejected.

### 4. Finish the message map (raise producer coverage) — [P1] [Effort: M]
**Why:** `index/message_edges.csv` has concrete consumers but mostly
`partial/unknown` producers; the last hop (use-case → topic) lives in a DB table.
**Where:** `index/` data + `RUNBOOK-3-message-map.md` + `retriever/messages.py`.
**Approach:** (a) Place the human-exported `tbl_event_router_usecase_topic.snapshot.csv`
into `index/` (dev/SCT — label it) so `usecase_route`/`trace` resolve.
(b) Resolve `TopicConfigEnum`/`MessageRouterTopicEnum` symbolic topics to their
literal strings from `mc-hk-hase-api-common` (these ARE in the mirror) and fill
`producer→destination` where provable. (c) Re-run and report the partial ratio.
**Done when:** `trace --use-case-id <real>` returns topic→consumer end-to-end for
a real use case, and the partial ratio in `message_edges.csv` drops measurably.

### 5. Scale the index beyond the 15-repo pilot — [P2] [Effort: L] **needs a decision**
**Goal:** Cover the whole estate, not just the ingress flow.
**Why:** Answers are only as good as what's mirrored/indexed.
**Where:** `group.py`, `harvest_poms.py`, mirror + CodeGraph.
**Approach:** Use `group.py <seed>` to form per-flow/domain bundles (~10–20 repos),
mirror + `codegraph init` each bundle separately (NOT one 390-repo graph — a
15-repo graph is already ~150 MB). Track disk/time. Decide bundle boundaries with
the maintainer. Keep the dependency + message graphs global (they already scale).
**Done when:** a second domain (e.g. `qcenter-*` or `campaign-*`) is queryable
with the same quality as ingress; disk/time documented.

### 6. Generate `index/REPOMAP.md` — [P2] [Effort: M]
**Why:** `prompts/qa-system-prompt.md` and RUNBOOK-2 reference a per-repo map to
"narrow first," but it may not exist yet.
**Where:** new `make_repomap.py` (or a RUNBOOK step), output `index/REPOMAP.md`.
**Approach:** For each mirrored repo: one-line purpose (from README + top package),
key entry points (main/controller/listener), and deps/dependents from
`internal_edges.csv`. Can be generated with the model in batch or heuristically.
**Done when:** `index/REPOMAP.md` has an entry per mirrored repo, ≤5 lines each,
cited where a file is named.

### 7. Freshness / re-index pipeline — [P2] [Effort: M]
**Why:** Mirror + graphs are point-in-time; answers go stale as repos change.
**Where:** new `refresh.py` + a scheduled runner (cron/Task Scheduler on the box).
**Approach:** Re-fetch changed repos (shallow), rebuild `internal_edges.csv` /
`message_edges.csv`, re-`codegraph init` affected bundles. Record a
`index/last_indexed.json` (timestamp + commit shas) and surface "indexed as of X"
in the UI. Idempotent, read-only.
**Done when:** running `refresh.py` updates the indexes and the UI shows the date.

### 8. Real streaming (live tool steps + token stream) — [P3] [Effort: L]
**Why:** Today the UI blocks on the full answer; the "Thinking…" pulse is fake.
**Where:** `webapp/server.py` (SSE `text/event-stream`), `webapp/agent.py`
(yield events), `webapp/llm_providers/*` (stream from the model), `index.html`.
**Approach:** Stream two event types: `tool` (name, as each runs) and `token`
(final-answer chunks). Keep the current non-streaming `/api/chat` working for the
eval harness; add `/api/chat/stream`. Provider streaming is per-provider (put it
in `llm_providers/*`, not `llm.py`).
**Done when:** tool chips appear live and the final answer types out; non-stream
path still works.

### 9. Team deployment & security hardening — [P3] [Effort: M] **needs a decision**
**Why:** It currently binds `127.0.0.1` with no auth — fine for one tester, not a team.
**Where:** `webapp/server.py`, config, deployment notes.
**Approach:** Optional bind `0.0.0.0`; put behind the org's SSO/reverse proxy;
add request logging/audit, basic rate limiting, and a max-concurrency guard
(ThreadingHTTPServer is unbounded). Confirm the data-governance story for a shared
internal service with the maintainer before exposing beyond localhost.
**Done when:** runs as a reviewed shared service with auth + audit; localhost mode
unchanged.

### 10. Usage / credit dashboard — [P3] [Effort: S]
**Why:** Per-answer `usage` exists; leadership will want aggregate spend.
**Where:** `webapp/llm_usage.py`, `session_store.py`, a small `GET /api/usage`.
**Approach:** Aggregate tokens + `total_nano_aiu` across sessions (per day / total).
Label nano-AIU as the copilot-api field, not dollars, unless a mapping is confirmed.
**Done when:** `/api/usage` returns totals and a simple page renders them.

### 11. Scaffolding generator (new-module "golden path") — [P4] [Effort: L]
**Goal:** "Create a new service for X" → scaffold that follows the shared
`api-parent` + `api-starter` conventions.
**Why:** The estate is one template repeated ~390 times — ideal for AI generation.
**Where:** new `scaffold/` + a tool/endpoint; reuse the retrieval layer for the
conventions.
**Approach:** Derive the golden template from `mc-hk-hase-api-starter` +
`mc-hk-hase-ingress-api` (a representative service), then generate a new module
skeleton (pom, package layout, headers/interceptors, a sample endpoint/listener).
Output to a scratch dir for human review — NEVER write into a production repo.
**Done when:** it produces a compiling-shaped module skeleton matching conventions,
into a scratch dir, with a diff for review.

### 12. Unified impact (sync + async + deps) — [P4] [Effort: M]
**Why:** `impact.py` is compile-time deps only; real blast radius also flows
through shared libs' call graphs and message queues.
**Where:** `retriever/` (new combined query), expose as a tool.
**Approach:** Given a symbol/repo, combine `internal_edges.csv` (deps),
`message_edges.csv` (async coupling), and CodeGraph callers into one ranked
"what could break" view, each edge labeled with its evidence.
**Done when:** the tool returns deps + message peers + callers for a seed, cited.

### Robustness (small, do anytime)
- **Prompt-based tool-calling fallback** in `agent.py` for endpoints without
  function-calling (parse a JSON action from text). [S]
- **Harden `copilot_responses.py`** against the real copilot-api response shape
  (verify `output`/`usage`/`copilot_usage` parsing with real payloads). [S]
- **Optional semantic search retriever** using the internal embedding model as a
  new tool, with the existing lexical `search_code` as the no-embedding fallback. [M]
