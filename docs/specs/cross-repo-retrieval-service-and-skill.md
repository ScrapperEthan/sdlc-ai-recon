# Spec — cross-repo retrieval service + `cross-repo-impact` skill

**Why.** Dev teams run the peer team's SDLC workflow in **GitHub Copilot Agent mode**. Their
agents are single-repo and doc-level RAG — they cannot see cross-repo blast radius or async
message routing. We already have that (the retrieval moat). To feed it into their Copilot agents,
we (A) expose our retrieval as a **read-only internal HTTP service**, and (B) ship a
**`cross-repo-impact` skill** in their SKILL.md format whose embedded commands `curl` that service
— mirroring exactly how their own `rag-data-search` curls an internal RAG URL.

This is the "our moat × their workflow" join point (see `docs/PEER-TEAM-ANALYSIS-zh.md` §6,
`docs/PEER-MEETING-PREP-zh.md` §3).

Delivery: **Part A → this repo** (`sdlc-ai-recon`), built by external Codex from this spec, then
verified on the box. **Part B → a separate new skills repo** (the SKILL.md draft below is
build-ready; drop it in as-is).

---

## Hard constraints (do not violate)

- **Preserve the existing Q&A app EXACTLY.** Do **not** modify `webapp/*`, `agent.py`, `llm.py`,
  `session_store`, or `static/*`. The `/api/chat`, `/api/chat/stream`, `agent.answer()` contracts
  and the `llm.py` facade stay untouched. The new service is a **standalone module** that imports
  only the `retriever/` package (the same functions `mcp_server.py` and `cli.py` already call).
  The Q&A app and this service run as **two separate processes**.
- **Read-only, stdlib-only, no egress, no auth (PoC).** Same posture as `webapp/server.py`
  (stdlib `http.server`). No new pip deps. Never write to `mirror/`.
- **No hardcoded internal endpoints in committed code.** The skill's service URL is an env
  placeholder (`RETRIEVAL_BASE_URL`), not a literal — unlike their hardcoded RAG.

---

## Part A — the retrieval HTTP service (`retrieval_service.py`, repo root)

A new stdlib `http.server` app that re-exposes the retrieval layer as read-only JSON over HTTP.
It is essentially `mcp_server.py`'s tool set, but reachable by a Copilot agent's `execute`/curl
without the MCP SDK (which is often pip-blocked on the box).

### Config (env, all optional)
- `RETRIEVAL_HOST` — default `127.0.0.1`. **For the shared PoC deployment, set to the box's LAN
  interface (e.g. `0.0.0.0`)** so dev machines can reach it. (PoC = no auth; document this.)
- `RETRIEVAL_PORT` — default `8848`.

### Endpoints (all GET; JSON out; errors as `{"error": "..."}` with 4xx/5xx)

| Route | Params | Calls | Returns |
|---|---|---|---|
| `/health` | — | — | `{"ok": true, "indexed_as_of": <from index/last_indexed.json or null>}` |
| `/impact` | `repo`, `transitive=0\|1` | `graph.impact(repo, transitive)` | dict |
| `/hubs` | `top=20` | `graph.hubs(top)` | list |
| `/consumers` | `destination` | `messages.who_consumes(destination)` | list |
| `/producers` | `destination` | `messages.who_produces(destination)` | list |
| `/repo-routes` | `repo` | `messages.routes_for_repo(repo)` | list |
| `/usecase` | `use_case_id`, `topic` | `messages.usecase_route(...)` | dict |
| `/search` | `pattern`, `glob=*.java`, `max=50` | `code.search_code(...)` | `{"results": [ "path:line:text", ... ]}` |
| `/read` | `path`, `start=1`, `end=` | `code.read_file(...)` | `{"path": ..., "text": ...}` |
| `/trace` | `use_case_id`, `destination` | `flow.trace(...)` | dict |
| `/repomap` | — | serve `index/REPOMAP.md` text | `text/plain` (for "narrow-first") |

`indexed_as_of` reuses the existing freshness file `index/last_indexed.json` (same one
`webapp/server.py`'s `/api/index-status` reads) — do not invent a new mechanism.

### Code sketch (dispatch table; keep it this small)
```python
# retrieval_service.py  — stdlib only, read-only
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from retriever import graph, messages, code, flow, config as rconfig

def _int(qs, k, d):  # single-value query int with default
    try: return int((qs.get(k) or [d])[0])
    except (ValueError, TypeError): return d
def _str(qs, k, d=""):
    return (qs.get(k) or [d])[0]

# route -> callable(qs) -> json-able  (search/read wrapped; see table)
ROUTES = {
  "/impact":      lambda qs: graph.impact(_str(qs,"repo"), _str(qs,"transitive") in ("1","true")),
  "/hubs":        lambda qs: graph.hubs(_int(qs,"top",20)),
  "/consumers":   lambda qs: messages.who_consumes(_str(qs,"destination")),
  "/producers":   lambda qs: messages.who_produces(_str(qs,"destination")),
  "/repo-routes": lambda qs: messages.routes_for_repo(_str(qs,"repo")),
  "/usecase":     lambda qs: messages.usecase_route(_str(qs,"use_case_id") or None, _str(qs,"topic") or None),
  "/search":      lambda qs: {"results": code.search_code(_str(qs,"pattern"), _str(qs,"glob","*.java"), _int(qs,"max",50))},
  "/read":        lambda qs: {"path": _str(qs,"path"), "text": code.read_file(_str(qs,"path"), _int(qs,"start",1), _int(qs,"end",0) or None)},
  "/trace":       lambda qs: flow.trace(_str(qs,"use_case_id") or None, _str(qs,"destination") or None),
}
```
`/health`, `/repomap` handled separately. Bad `repo`/path → catch `ValueError`/`FileNotFoundError`
→ `{"error": ...}` with 400/404 (same shape `webapp/server.py` uses). Method != GET → 405.

### Acceptance criteria
- `python retrieval_service.py` starts; `GET /health` → `{"ok":true, "indexed_as_of": ...}`.
- `GET /impact?repo=<a real mirror repo>&transitive=1` returns the same JSON as
  `python cli.py impact <repo> --transitive`.
- The **Q&A app still runs unchanged** on its own port at the same time (prove both up together).
- No writes to `mirror/`; no new pip deps; runs under the box's Python.
- Unit test (`tests/test_retrieval_service.py`): drive the handler / route table against a tiny
  fixture graph, assert JSON shape for `impact`, `consumers`, `trace`, and a 404 on a bad repo.

---

## Part B — the `cross-repo-impact` skill (goes in the separate skills repo)

Build-ready `SKILL.md`, authored in the peer team's format (folder `skills/cross-repo-impact/`,
YAML frontmatter `name`/`description`/`argument-hint`, no `tools:` block — the **agent** supplies
`execute`). It curls Part A and writes a cited impact artifact the Plan/Review step consumes.

```markdown
---
name: cross-repo-impact
description: Given a target repo, use-case, or topic, fetch cross-repo blast radius
  (dependents, async producers/consumers, routing) from the HASE retrieval service
  and write a cited CROSS_REPO_IMPACT_<flow>.md the planner/reviewer can use.
argument-hint: "<repo> | use-case:<id> | topic:<name>"
---

# cross-repo-impact

You analyze the **cross-repo** blast radius of a change before planning or reviewing it, using
the HASE retrieval service (the estate-wide dependency graph, message map, and code index).
Single-repo reading CANNOT see this; always consult the service.

## Inputs
- A target: a repo name, or `use-case:<id>`, or `topic:<name>`.
- Service base URL from env `RETRIEVAL_BASE_URL` (e.g. an internal `http://host:8848`).

## Steps (use your `execute` tool; do not guess — call the service)
1. If the target is a **repo**:
   - `curl -s "$RETRIEVAL_BASE_URL/impact?repo=<repo>&transitive=1"`  → downstream dependents (blast radius).
   - `curl -s "$RETRIEVAL_BASE_URL/repo-routes?repo=<repo>"`          → async queues/topics this repo touches.
2. For each queue/topic found (or if target is a **topic**):
   - `curl -s "$RETRIEVAL_BASE_URL/producers?destination=<dest>"` and `/consumers?destination=<dest>`
     → who else is on that async route (the hidden cross-repo coupling).
3. If the target is a **use-case**: `curl -s "$RETRIEVAL_BASE_URL/trace?use_case_id=<id>"`.
4. To ground a claim in source: `curl -s "$RETRIEVAL_BASE_URL/search?pattern=<sym>&glob=*.java"`
   then `/read?path=<path>&start=<n>&end=<m>`.

## Output — write `CROSS_REPO_IMPACT_<flow-id>.md`
- **Blast radius**: repos that depend on the target (direct + transitive).
- **Async coupling**: producers/consumers sharing each topic/queue the target touches.
- **Risk callouts**: hub repos in the path; use-cases whose routing is only partly provable
  from source (say so honestly — routing lives in a DB table, not code).
- **Citations**: every claim carries `repo/path:line`. Do not invent paths; if the service
  didn't return it, don't assert it.

## Guardrails
- Read-only. You never modify the estate; you only query the service and write the .md artifact.
- If the service is unreachable, STOP and say so — do not fall back to guessing cross-repo impact.
```

### How it slots into the workflow
Run `cross-repo-impact` **before** the coding/planning step and pass
`CROSS_REPO_IMPACT_<flow>.md` in as planning context; optionally run it again at **review** time
so the reviewer checks downstream consumers/producers. In the peer flow that's "before the Plan
agent plans" and "before the Review agent reviews."

> Note: we deliberately do **not** reuse their PIB-specific coding skills (`domain-papi-*`) — the
> coding step is where our own `change/` pipeline (intent → change → compile+test → diff) is the
> stronger, differentiated answer. `cross-repo-impact` is the piece that makes *any* coding agent
> (theirs, plain Copilot, or ours) cross-repo-aware.

---

## Verification (internal Codex, after Part A is built + pulled)
1. Start `retrieval_service.py` against the real `mirror/`+`index/`; confirm the Q&A app also
   still starts (both processes up).
2. `GET /impact`, `/consumers`, `/trace` on real inputs return sane JSON matching `cli.py`.
3. Set `RETRIEVAL_BASE_URL` to the running service; run the `cross-repo-impact` skill on one real
   target under whatever agent runtime the box uses; confirm it produces a cited
   `CROSS_REPO_IMPACT_*.md` and touches nothing in `mirror/`.
4. Report: both-up? endpoint parity with `cli.py`? artifact produced + citations resolve?
