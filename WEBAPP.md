# Web Q&A app — developers open a browser, no opencode needed

Thin chat app over the retrieval layer. Standard library only (no pip), read-only.

```
browser  ->  webapp/server.py  ->  webapp/agent.py (tool loop)  ->  webapp/llm.py  ->  GPT-5.5
                                              -> webapp/tools.py -> retriever/ + CodeGraph
```

## Codex: your one job is `webapp/llm.py`

That file is the ONLY place that talks to the model. Set three env vars and
confirm the auth header / tool-calling support — nothing else needs changing:

```
LLM_BASE_URL   e.g. https://<internal-gpt-5.5>/v1
LLM_API_KEY    token (or adjust the auth header in llm.py if the gateway differs)
LLM_MODEL      e.g. gpt-5.5
```

The app sends OpenAI-style `tools` and reads `message.tool_calls`. If the
endpoint doesn't support function-calling, say so — agent.py has a documented
prompt-based fallback that needs no other changes.

## Run it (from the workspace root, where mirror/ recon_out/ index/ live)

```bash
# 1) plumbing test FIRST — no model required (canned loop, exercises UI + tools):
LLM_MOCK=1 python -m webapp.server
#    open http://127.0.0.1:8765  and ask anything → you should see a tool run.

# 2) real answers — after Codex wires the model:
LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=gpt-5.5 python -m webapp.server
```

Serve to a team: set `SDLC_HOST=0.0.0.0` and a port, run it on one internal box;
developers just open the URL. Nobody installs opencode or anything.

## Notes

- **LLM providers (merge-conflict rule):** `webapp/llm.py` is a small stable
  facade — do NOT edit it for provider work. Provider code lives in
  `webapp/llm_providers/`: `copilot_responses.py` (local copilot-api `/responses`,
  the default) and `openai_chat.py` (standard `/chat/completions`). Pick with
  `LLM_PROVIDER`. Put provider/protocol/network changes in those files so
  `git pull` stays fast-forward. Per-question token/credit usage is aggregated in
  `webapp/llm_usage.py` and returned as `result["usage"]`.
- The assistant's behaviour/citation rules come from `prompts/qa-system-prompt.md`.
- It only reads `mirror/`, `recon_out/`, `index/`. No DB, no credentials.
- Chat sessions are stored locally in JSON at `webapp_data/chat_sessions.json` by default.
- Override the session file path with `SDLC_SESSION_STORE=/path/to/chat_sessions.json`.
- **Session titles** are written by the model (`webapp/session_title.py`) on the FIRST exchange of a
  session — one short extra model call per session, never per turn — so the sidebar says
  "3HK SMSC 投递失败" instead of the first 48 characters of a question that starts the same way as
  every other one. Any failure (model down, mock mode, junk reply) falls back to that truncation.
  `SDLC_SESSION_TITLE_LLM=0` turns the model call off and keeps the truncation.
- **Session search**: `GET /api/sessions?q=<text>` scans the caller's own sessions (titles + message
  text, case-insensitive substring — no index, no model) and returns the same shape as the plain
  listing plus a `match` field saying where it hit and the surrounding text. The sidebar search box
  drives it.
- **查看原文** (retained raw log lines) opens the full-height `#rawlog-panel` drawer — same geometry
  and Escape-to-close as the citation source viewer — with line numbers and a wrap toggle. It used
  to be an inline pane nested inside the 260px step list, i.e. a scroll box inside a scroll box
  showing a handful of an up-to-500-line log. No copy button, deliberately: the text is selectable,
  and one-click "production log lines to clipboard" is not something this UAT-only path should add.
- **Telling the investigator where to look**: `incident_investigate` takes `repos`. Targets were
  otherwise derived only from repo ids appearing in the alert TEXT, so an alert that names no
  service (the largest family here) refused even when the agent already knew the repo from
  `incident_impact`. Supplied ids are validated against the same repo universe the text scan uses —
  an unknown id refuses rather than aiming production reads at the wrong service — and carry
  `plan.targets[].source: "supplied by the caller"` so a nil result keeps its weaker provenance.
  The tool is re-callable within one turn; the schema and system prompt name the signals that mean
  "sweep again" and the one refusal (blocking window) that only the user can clear.
- **Clickable tool trace** (`webapp/tool_trace.py`): every chip under an answer is a button over one
  ledger entry — click it for that call's arguments, its output, how long it took, and, when it
  failed, `failure_class`, which ARGUMENT was wrong (`field` / `expected` / `actual_type`, all taken
  from the tool's own schema) and who can unblock it. The output shown is the exact string the model
  was handed, plus the untruncated result size beside it, so a shortened result never reads as the
  whole one. Two failure classes are new gates rather than reports: unreadable tool-call JSON
  (`bad_call_syntax`) and a missing/unusable required argument (`bad_arguments`) are caught BEFORE
  dispatch and fed back to the model as structured fields, which is what lets the turn repair itself
  instead of dying on `{"error": "'repo'"}`. Anything unclassified is `internal_error` — never
  `bad_arguments`, and never pointed at the user. Type mismatches the tools already tolerate (a
  `"50"` where an integer belongs) still dispatch unchanged and are recorded as notes, so nothing
  that worked before now fails. Cap the stored copy with `SDLC_TRACE_OUTPUT_CHARS` (default 4000
  characters per call).
- Incident-investigator progress steps are saved with the assistant message (`subagent_steps`) and
  re-rendered on reload. They are the already-sanitized stream events, and `history_for_agent` sends
  the model role+content only — so replaying them reaches the browser, never the model. Raw log text
  still lives solely in the separate owner-scoped store (`SDLC_INCIDENT_RAW_LOGS`).
- `call_graph` shells out to the `codegraph` CLI if present (synchronous
  who-calls-whom); everything else is pure retrieval-layer.
- Tune with env: `SDLC_MAX_TOOL_ITERS`, `SDLC_TOOL_RESULT_CAP`, `SDLC_PORT`, `SDLC_HOST`.
