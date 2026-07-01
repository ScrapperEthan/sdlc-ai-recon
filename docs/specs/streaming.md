# Spec: real streaming (live tool steps + token-by-token answer)

**For the implementer (external Codex):** build this from the spec below. It is
self-contained; read the "Project context" and "Guardrails" in `BACKLOG.md` first.
**For the verifier (internal Codex):** run the "Acceptance" + "Verification" steps
against the real copilot-api after pull.

Keep the non-streaming `/api/chat` working unchanged (the eval harness and old
clients depend on it). Add streaming alongside it.

## Current flow (what exists today)

- `webapp/server.py` → `POST /api/chat` reads `{question, session_id}`, calls
  `agent.answer(question, history)` (BLOCKING: runs the whole tool loop + every
  model call), then sends the full JSON `{answer, tool_trace, usage, session}`.
- `webapp/agent.py::answer()` = the tool loop: `llm.chat(messages, tools.TOOLS)`
  → if `tool_calls`, run each via `tools.dispatch`, append results, repeat → else
  return the final message. Usage aggregated via `llm_usage.add_call`.
- `webapp/llm.py` facade → `llm_providers/{copilot_responses,openai_chat}.py::chat`.
- Frontend `index.html::ask()` does `await fetch('/api/chat')` then renders once;
  the "Thinking…" pulse is cosmetic (NOT streaming).

## Goal — two visible layers

1. **Tool steps live:** as each retrieval tool runs, a chip appears immediately
   ("Searching code… / impact… / trace…"), before the answer exists.
2. **Answer types out:** the final answer streams token-by-token.

## Design decisions (already made — don't re-litigate)

- **Transport: NDJSON over a chunked POST response** (not SSE/`EventSource`).
  `EventSource` is GET-only; we must POST the question. So: `POST /api/chat/stream`
  returns `Content-Type: application/x-ndjson`, one JSON object per line, flushed
  as produced. The browser reads it with `fetch()` + `response.body.getReader()`.
- **Reuse the tool loop.** Refactor the loop into a GENERATOR that yields events;
  `answer()` becomes a thin consumer of it so both paths share one implementation
  (no logic duplication, evals stay valid).
- **Provider streaming is optional.** Token streaming of the final answer is a
  per-provider enhancement in `llm_providers/*`. If a provider doesn't implement
  it, the facade falls back to a non-stream call + chunking the string into token
  events — so the "typing" UX works from day one, real provider streaming lands later.
- **Merge rule holds:** provider streaming code goes in `llm_providers/*`, never in `llm.py`.

## Event protocol (`/api/chat/stream`, one JSON per line)

```
{"type":"tool_start","name":"impact","args":{...}}
{"type":"tool_end","name":"impact"}
{"type":"token","text":"..."}                       # 0..N, in order
{"type":"done","answer":"<full>","tool_trace":[...],"usage":{...},"session":{...}}
{"type":"error","error":"..."}                       # terminal; may appear instead of done
```
`done` carries the same fields `/api/chat` returns today, so the frontend can
persist/refresh identically. `answer` on `done` = the full concatenated text
(source of truth; the client may rebuild from tokens but should trust `done`).

## Backend

### 1) `webapp/agent.py` — turn the loop into a generator

```python
def answer_events(question, history=None):
    """Yield protocol events; finish with a 'done' (or 'error') event."""
    messages = [{"role": "system", "content": _system_prompt()}]
    messages += history or []
    messages.append({"role": "user", "content": question})

    trace, usage = [], llm_usage.empty_usage()
    for _ in range(config.MAX_TOOL_ITERS):
        message = llm.chat(messages, tools.TOOLS)      # tool-deciding call: non-stream
        llm_usage.add_call(usage, message)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            # final answer: stream it (real tokens if the provider supports it,
            # else chunked fallback — see llm.stream_text)
            for chunk in llm.stream_text(message):
                yield {"type": "token", "text": chunk}
            yield {"type": "done", "answer": message.get("content") or "",
                   "tool_trace": trace, "usage": usage}
            return
        for call in calls:
            fn = call.get("function", {}); name = fn.get("name", "")
            try: args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError: args = {}
            yield {"type": "tool_start", "name": name, "args": args}
            try: result = tools.dispatch(name, args)
            except Exception as e: result = {"error": str(e)}
            yield {"type": "tool_end", "name": name}
            trace.append({"tool": name, "args": args})
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": json.dumps(result, ensure_ascii=False)[:config.TOOL_RESULT_CAP]})

    messages.append({"role": "user", "content": "Answer now with what you have; mark unverified as partial."})
    message = llm.chat(messages); llm_usage.add_call(usage, message)
    for chunk in llm.stream_text(message):
        yield {"type": "token", "text": chunk}
    yield {"type": "done", "answer": message.get("content") or "", "tool_trace": trace, "usage": usage}
```

Then make the existing blocking API reuse it (no duplicated logic):

```python
def answer(question, history=None):
    answer_text, trace, usage = "", [], {}
    for ev in answer_events(question, history):
        if ev["type"] == "done":
            answer_text, trace, usage = ev["answer"], ev["tool_trace"], ev["usage"]
    return {"answer": answer_text, "tool_trace": trace, "usage": usage}
```

> Note: streaming the FINAL call this way (buffer message, then re-emit) gives the
> chunked-fallback behaviour for free. To stream REAL tokens from the model on the
> final call, `llm.stream_text` must call a provider that streams (below). Content
> tokens only ever appear on the final call, so this is safe.

### 2) `webapp/llm.py` — add a streaming helper (facade, stays thin)

```python
def stream_text(message):
    """Yield the assistant text in chunks. Real streaming if the provider offers
    it; otherwise chunk the already-produced content so the UI still types."""
    provider = _provider_module()          # small dispatch, mirrors chat()
    streamer = getattr(provider, "stream_text", None)
    if streamer and message.get("_stream_handle"):
        yield from streamer(message)
        return
    text = message.get("content") or ""
    for i in range(0, len(text), 24):      # fallback: ~24-char chunks
        yield text[i:i + 24]
```

Keep the fallback the default. Real provider streaming (a `stream_text` in
`llm_providers/*` plus a streaming `chat`) is a SEPARATE follow-up — spec it only
if the maintainer asks; the copilot-api `/responses` streaming event shape must be
verified against the live endpoint first (internal Codex).

### 3) `webapp/server.py` — the streaming endpoint

Add to `do_POST` (alongside `/api/chat`):

```python
if path == "/api/chat/stream":
    question = (req.get("question") or "").strip()
    if not question:
        self._send_json(400, {"error": "Question is required"}); return
    session_id = req.get("session_id") or session_store.create_session()["id"]
    try: history = session_store.history_for_agent(session_id)
    except KeyError: self._send_json(404, {"error": "Session not found"}); return

    self.send_response(200)
    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    self.send_header("Cache-Control", "no-cache")
    self.send_header("X-Accel-Buffering", "no")     # disable proxy buffering
    self.send_header("Connection", "close")
    self.end_headers()

    answer_text, trace, usage = "", [], {}
    def emit(obj):
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()
    try:
        for ev in agent.answer_events(question, history):
            if ev["type"] == "done":
                answer_text, trace, usage = ev["answer"], ev["tool_trace"], ev["usage"]
                session = session_store.append_exchange(session_id, question, answer_text, trace, usage)
                ev["session"] = {"id": session["id"], "title": session["title"],
                                 "created_at": session["created_at"], "updated_at": session["updated_at"],
                                 "message_count": session["message_count"]}
            emit(ev)
    except Exception as e:  # noqa: BLE001
        try: emit({"type": "error", "error": str(e)})
        except Exception: pass
    return
```

Notes: don't set `Content-Length` (streaming). `Connection: close` + close-delimited
body is fine for the stdlib `http.server` (client reads to EOF). Persist the session
only on `done` (after the full answer exists), exactly like the blocking path.

## Frontend (`webapp/static/index.html`)

Add `askStream(text)` and make the composer call it (keep `ask()` as a fallback
you can toggle). Reuse existing helpers `add`, `addToolTrace`, `renderUsage`,
`setBubbleContent`, `decorateAnswer`, `refreshSessions`.

```js
async function askStream(text) {
  add('user', text, 'you');
  history.push({role:'user', content:text});
  const pending = add('assistant', '', 'assistant');
  const bubble = pending.querySelector('.bubble');
  bubble.classList.add('loading'); bubble.textContent = 'Working…';
  setBusy(true);
  const trace = [];
  let answer = '', started = false;
  try {
    const r = await fetch('/api/chat/stream', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question:text, session_id: currentSessionId})});
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
    for (;;) {
      const {value, done} = await reader.read(); if (done) break;
      buf += dec.decode(value, {stream:true});
      let nl; while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === 'tool_start') { trace.push({tool: ev.name}); showLiveTools(pending, trace); }
        else if (ev.type === 'token') {
          if (!started) { bubble.classList.remove('loading'); bubble.textContent = ''; started = true; }
          answer += ev.text; bubble.textContent = answer;      // plain text while typing
          log.scrollTop = log.scrollHeight;
        }
        else if (ev.type === 'done') {
          answer = ev.answer || answer;
          setBubbleContent(bubble, answer || '(empty)', true);  // final: markdown + citation pills + evidence fold
          history.push({role:'assistant', content: answer});
          if (ev.usage) renderUsage(pending, ev.usage);
          if (ev.session) { currentSessionId = ev.session.id; await refreshSessions(currentSessionId); }
        }
        else if (ev.type === 'error') {
          bubble.classList.remove('loading');
          setBubbleContent(bubble, 'Error: ' + ev.error, false);
        }
      }
    }
  } catch (e) {
    bubble.classList.remove('loading'); setBubbleContent(bubble, 'Request failed: ' + e, false);
  }
  setBusy(false); q.focus();
}
```

Add a tiny `showLiveTools(container, trace)` that renders/updates a live version
of the tool chips (reuse `addToolTrace`'s chip markup; replace the existing
`.tools` block on each update, or append incrementally). During typing render the
bubble as PLAIN text (fast); only on `done` call `setBubbleContent(..., true)` so
`decorateAnswer` adds the citation pills + folds the Evidence section.

## Keep working (regression list)

- `POST /api/chat` (blocking) unchanged — evals + any old client still pass.
- Sessions still persist the final answer + `usage`; reload renders identically.
- `usage` still shows; `decorateAnswer` still styles citations/evidence on `done`.
- `LLM_MOCK=1` still works end-to-end (mock `chat` + chunked `stream_text`).

## Acceptance criteria

1. Asking a question over `/api/chat/stream`: tool chips appear BEFORE any answer
   text; then the answer types out; final render has citation pills + evidence fold.
2. `curl -N` on `/api/chat/stream` shows newline-delimited `tool_start/token/done`
   events streaming over time (not all at once at the end).
3. `/api/chat` still returns the full blocking JSON unchanged.
4. A mid-stream provider error yields a terminal `{"type":"error"}` and the UI
   shows it (no hang).
5. Session saved once, on `done`; reloading the session shows the same answer + usage.

## Verification steps (internal Codex, real copilot-api)

```bash
# blocking path unaffected
python -B -c "import webapp.agent, webapp.server; print('imports ok')"
# stream path emits events over time (watch them arrive one by one)
curl -N -s http://127.0.0.1:8765/api/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"question":"Which repos consume otx_bat_letter?"}'
```
Expect: `tool_start`/`tool_end` lines first, then `token` lines, then one `done`.
Then in the browser: chips live, answer types, evidence pills on completion.

## Risks / notes

- **Proxy buffering** can defeat streaming. `X-Accel-Buffering: no` handles nginx;
  if fronted by something else, disable response buffering there too.
- **Real token streaming from copilot-api** (`/responses` streaming events) is the
  optional follow-up — its event shape must be confirmed against the live endpoint.
  Until then the chunked fallback gives the typing effect. Put any real streaming
  parse in `llm_providers/copilot_responses.py::stream_text`, not in `llm.py`.
- Do not stream the intermediate tool-deciding calls' content — only the final
  answer produces user-facing tokens (the generator already enforces this).
