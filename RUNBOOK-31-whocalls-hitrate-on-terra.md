# RUNBOOK 31 (INTERNAL Codex / you) — measure the "who calls" first-answer hit-rate on gpt-5.6-terra

> **Who runs: you or INTERNAL Codex on the box.** **Pull `master` first**, then run a small probe
> a few times. **No code change to decide here** — we're gathering data. RUNBOOK-30 showed the
> campaign-core caller with a line (`:51`/`:45-52`) is now a *model-behaviour* question, not a
> source/plumbing one (source is readable, refs verify green). That single run was also on a
> just-swapped model (gpt-5.5 → **gpt-5.6-terra**). Before touching the prompt or the retrieval
> path, measure how often the first answer actually pins the caller, across two phrasings.

## Model / config note (do this once)
Stop maintaining a local `webapp/config.py` edit for the model — it forces a stash/restore on every
pull. `config.py` is env-driven by design, so instead:
```
setx LLM_MODEL gpt-5.6-terra          (user env var, like SDLC_MIRROR / LLM_STREAM)
git checkout -- webapp/config.py       (drop the local edit so pulls are clean)
```
Then open a fresh terminal so the env var is picked up.

## Step 1 — pull + start the webapp on the full source, streaming on
```
git pull
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
set LLM_STREAM=1
python -m webapp.server
```
(Retrieval on :8848 can stay up from before.)

## Step 2 — run the probe
`probe_whocalls.py` fires the same "who calls IngressService" question N times per phrasing and
tallies how often the FIRST answer surfaces the `campaign-core` caller, and how often it does so
**with a line number** (`SendCampaignEventService.java:<n>`). It checks both the answer text and the
verified citations.
```
python probe_whocalls.py --n 5
```
It prints, per phrasing:
- `terse`    — the bare "谁调用了 IngressService？" (the ambiguous one that flaked)
- `explicit` — "…列出真实的跨仓库调用者，并给出每个调用者的源码 file:line。"
and a tally line like `== terse: caller 3/5, with :line 2/5`.

Cost: 2 × N model calls (N=5 → 10 calls). Bump/lower `--n` as you like.

## Step 3 — send back
```
Model:     [ confirm LLM_MODEL=gpt-5.6-terra picked up ]
Probe:     [ paste the two "== …: caller X/N, with :line Y/N" lines for terse and explicit ]
Feel:      [ eyeballing a couple of the explicit answers — is the caller list + the diagram good? ]
```

## What we'll do with it
- If **explicit** is reliably high (e.g. 5/5 with :line) and only **terse** flakes → the fix is just
  to use the unambiguous phrasing in the demo; no code change.
- If **explicit** also flakes on gpt-5.6-terra → we make it deterministic on the retrieval side (for
  who-calls questions the backend always runs `unified_impact` and injects the caller list + lines so
  the model can't miss it), rather than fighting it in the prompt.

Either way we decide from the numbers, not one run.
