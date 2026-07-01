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
- `call_graph` shells out to the `codegraph` CLI if present (synchronous
  who-calls-whom); everything else is pure retrieval-layer.
- Tune with env: `SDLC_MAX_TOOL_ITERS`, `SDLC_TOOL_RESULT_CAP`, `SDLC_PORT`, `SDLC_HOST`.
