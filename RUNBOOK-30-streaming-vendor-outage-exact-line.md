# RUNBOOK 30 (INTERNAL Codex / you) — turn on true streaming, verify the narrowed vendor-outage + exact call-line citation

> **Who runs: you or INTERNAL Codex on the box.** **Pull `master` first**, then restart both
> services so the new code loads. Three demo-polish changes from the last review:
> (A) **true token streaming** for the Q&A answer (opt-in, `LLM_STREAM=1`) so it types out live
> instead of a ~40 s "working" then a dump; (B) **vendor-level outage no longer over-counts** — a
> single-vendor outage (Sinch) was reporting the whole SMS channel (118); now it's the vendor's own
> repos + downstream; (C) the Q&A **first answer cites the actual call line** (`:51` / `:45-52`), not
> just the method header (`:45`). (B) and (C) are automatic after pull+restart; (A) is a flag you flip.

## What changed in the repo
- `outage_report.py` — `build_report` folds the channel blast-radius **only for `channel:` targets**;
  `vendor:`/`repo:` targets scope to that target's delivery+outbound repos and their dependency
  closure. (New test: `tests/test_outage_impact.py::test_vendor_outage_excludes_channel_blast_radius`.)
- `webapp/llm_providers/copilot_responses.py` — new `chat_stream` (+ `_iter_sse_events`): a
  `stream:true` Responses call parsed as SSE; the authoritative message comes from the terminal
  `response.completed` event (so a wrong delta name only costs the live typing, never the answer).
- `webapp/llm.py` — `chat_stream` facade: streams only when `LLM_STREAM=1` **and** the provider
  supports it; **any** failure falls back to the blocking `chat` (no regression).
- `webapp/agent.py` — the tool loop consumes `chat_stream`, emitting `token` events as deltas arrive.
- `webapp/config.py` — `LLM_STREAM` (default **off**).
- `prompts/qa-system-prompt.md` — cite the invocation line, not the enclosing method header.
- Tests: `tests/test_llm_stream.py` (SSE parser + facade fallback, all local). Full suite **83 pass**.

## Step 1 — pull + restart
```
git pull
git rev-parse HEAD          # note it; git log --oneline -3 should show the streaming/outage commit on top
```
Restart retrieval (:8848) and webapp (:8765) with the full-source env (same as before):
```
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
set RETRIEVAL_PORT=8848
python retrieval_service.py
```
new terminal:
```
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
python -m webapp.server
```
> (Use whatever `SDLC_MIRROR` path actually works on your box — last time you confirmed
> `C:\Users\45589915\Downloads\HASE_MDC`. If that user id is still wrong, use the real one.)

## Step 2 — verify (B) vendor outage is no longer channel-wide
```
python outage_report.py vendor:sinch
```
Expect the affected-repo count to **drop from 118 to ~20-25**, and `by_relation` to contain **only**
`delivery-job` / `outbound-api` / `dependency-downstream` (+ maybe `dependency-upstream`) — **no more
`channel-owner` / `serves-channel` / `msg-channel`**, and no CSL/CM `*-deli-job` in the list (those are
other vendors). Then in the browser: `arch.html` → click the **Sinch 供应商** node → 查故障影响 → same
smaller, Sinch-only number.
> Sanity: clicking the **SMS 投递任务** (channel) node still gives the full channel blast-radius — that
> distinction (vendor vs channel) is now the point, not a bug.

## Step 3 — verify (C) the first answer cites the call line
On `:8765` re-ask **"谁调用了 IngressService？"**. In `## Evidence`, the campaign-core caller should
now cite the **invocation** line — `SendCampaignEventService.java:51` (or a range like `:45-52` that
contains 51) — not a bare `:45` method header.

## Step 4 — turn ON true streaming and verify
Stop the webapp, relaunch it with the flag:
```
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
set LLM_STREAM=1
python -m webapp.server
```
Ask any question. **Watch the answer bubble: the text should now appear token-by-token as it's
generated** (the retrieval steps still stream as before; now the prose does too), instead of a long
"working" then the whole answer at once.

- **If it types out live → streaming works, keep `LLM_STREAM=1`** (make it a permanent user env var).
- **If the answer is empty, garbled, or errors** → the local copilot-api probably doesn't emit
  standard Responses SSE. **Just unset `LLM_STREAM`** (or set it to `0`) and relaunch — you're back to
  today's exact behavior, no harm done. Then paste what the stream looked like (see the probe below)
  so we can adapt the parser.

Probe whether copilot-api streams at all (optional, helps us if Step 4 misbehaves):
```
curl -N -s http://127.0.0.1:4141/v1/responses -H "Content-Type: application/json" ^
  -d "{\"model\":\"gpt-5.5\",\"input\":[{\"role\":\"user\",\"content\":\"say hi\"}],\"stream\":true}" | more
```
We want to see `text/event-stream` lines like `data: {"type":"response.output_text.delta",...}` and a
final `data: {"type":"response.completed",...}`. Paste the first ~15 lines.

## Send back
```
Step 1:  git rev-parse HEAD           [ hash ]
Step 2:  outage_report vendor:sinch    [ affected count + by_relation keys ]
Step 3:  IngressService first answer   [ campaign-core Evidence line — is it :51 / :45-52 now? ]
Step 4:  LLM_STREAM=1                   [ does the answer type out live? if not, paste the curl probe's first lines ]
```

## What this establishes
The demo answers now stream like a normal chat assistant (no dead 40 s wait), a single-vendor outage
reports an honest blast radius instead of the whole channel, and "who calls X" cites the exact call
line — three things a leadership audience will notice.
