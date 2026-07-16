# RUNBOOK 33 (INTERNAL Codex / you) — verify the INLINE architecture view inside the Q&A answer (Phase B)

> **Who runs: you or INTERNAL Codex on the box.** **Pull `master` first**, then restart BOTH services
> (the webapp reloads the new tool + agent code; the retrieval service serves the embedded diagram the
> chat iframes in). This is the first slice of "the user never leaves the chat": when someone asks what a
> **channel or vendor** problem affects, the assistant now renders the architecture diagram **inline in
> its answer** with the affected chain highlighted — no page switch, no clicking a node.

## What changed
- `retriever/arch_focus.py` — resolves `channel:X` / `vendor:Y` to the affected node set (same rule as
  the page: vendor = that vendor's chain only, not the whole channel).
- `webapp/tools.py` — new `show_arch(kind, value)` tool → returns a view directive
  (`/arch.html?embed=1&highlight=…`).
- `webapp/agent.py` — when the model calls `show_arch`, emits a `view` stream event.
- `webapp/static/index.html` — on a `view` event, embeds `RETRIEVAL_BASE + url` as an inline iframe in
  the answer (defaults to `http://<host>:8848`).
- `static/arch.html` — `?embed=1` strips the page chrome to just the highlighted diagram.
- `prompts/qa-system-prompt.md` — tells the model to call `show_arch` for channel/vendor outage Qs.
- Tests: `tests/test_arch_focus.py`. Full suite **89 pass** locally. (I verified the embed page, the
  highlight sets, and the inline-iframe wiring locally; the only thing I can't test off-box is the real
  model actually calling `show_arch` — that's this runbook.)

## Step 1 — pull + restart both
```
git pull
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
set RETRIEVAL_PORT=8848
python retrieval_service.py
```
new terminal:
```
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
set LLM_STREAM=1
python -m webapp.server
```
> The chat (:8765) iframes the diagram from the retrieval service (:8848), so **:8848 must be up**.
> If your browser is not on the same host, set `LLM`… no — just make sure both are on 127.0.0.1.

## Step 2 — sanity-check the tool directly (no model)
```
python -c "from retriever import arch_focus, json; print(json.dumps(arch_focus.focus('vendor','sinch'),ensure_ascii=False))"
```
Expect `ok: true`, `url: /arch.html?embed=1&highlight=vendor:sinch`, `affected_node_ids` containing
`ext-sinch`/`sms-sinch` but NOT `sms-csl`. Also open in a browser to eyeball the embedded view:
`http://127.0.0.1:8848/arch.html?embed=1&highlight=vendor:sinch` → just the diagram, Sinch chain red.

## Step 3 — the real thing: ask in plain language on :8765
Ask these (as a clueless user would), and watch the answer:

1. **"Sinch 出问题了，严重吗？影响哪些？"**
   → the answer text should say it's the vendor-level blast radius (~24, not the whole SMS channel),
   AND **an architecture diagram should appear inline in the reply** with only Sinch's chain highlighted.
2. **"短信 SMS 发不出去了，会影响什么？"**
   → inline diagram with the whole SMS lane (incl. CSL/3HK) highlighted.

Confirm:
- the diagram shows up **inside the chat answer** (not a link you click);
- the tool trace shows `show_arch` was called;
- for the vendor question, only Sinch's chain is lit (CSL/3HK dim).

## If the diagram does NOT appear
- Check the tool trace / logs for whether the model called `show_arch`. If it didn't, the text answer
  still works — the model just skipped the tool. Tell me the hit-rate across a few asks; if it's flaky
  we make the injection deterministic (same plan as the caller list).
- If `show_arch` WAS called but no iframe shows: open the browser devtools console on :8765 and check for
  an iframe load error (X-Frame-Options / mixed origin). Paste it and I'll adjust.

## Send back
```
Step 2:  arch_focus.focus('vendor','sinch')   [ ok? url? ]
Step 3:  Sinch question   [ screenshot: did the inline highlighted diagram appear in the answer? show_arch called? ]
Step 3:  SMS question     [ screenshot ]
Hit-rate: [ across ~4 asks, how often did the inline diagram appear? ]
```

## What this sets up
This is Phase B of the assistant-driven console: the views come to the user inside the chat. Next:
Phase C folds the standalone pages into one shell, and the deterministic caller injection makes the
who-calls diagram complete every time.
