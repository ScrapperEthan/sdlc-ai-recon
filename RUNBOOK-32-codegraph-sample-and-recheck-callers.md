# RUNBOOK 32 (INTERNAL Codex / you) — grab a raw CodeGraph caller sample + re-measure the caller-list completeness

> **Who runs: you or INTERNAL Codex on the box.** **Pull `master` first.** Two quick things, no code
> changes to decide: (1) dump the **raw `codegraph explore IngressService` output** so we can build a
> reliable parser for a *deterministic, never-drops-a-caller* who-calls answer; (2) **re-run the probe**
> to see whether the last prompt tweak ("enumerate EVERY caller") steadied the caller COUNT (the key
> campaign-core caller is already 5/5; only the total count was wobbling). The env vars from RUNBOOK-31
> (`LLM_MODEL=gpt-5.6-terra`, `LLM_STREAM=1`, `SDLC_MIRROR=…`) should still be set.

## Step 1 — pull
```
git pull            # picks up the prompt tweak + dump_callgraph.py
```
(No restart needed for the prompt — it's read fresh per question. Keep retrieval :8848 and webapp :8765 up; the webapp already reloads the prompt each ask.)

## Step 2 — dump the raw CodeGraph caller block for IngressService
```
python dump_callgraph.py IngressService
```
This routes the symbol to its built bundle (same path the Q&A uses) and prints:
- a header: `available=… returncode=… bundle_root=…` (we want `available=True` and a real bundle_root)
- `output_len=…`
- the **first 45 lines of the raw `codegraph explore IngressService` output**

and saves the full block to `index/reports/CG_IngressService.json`.

**Send back the printed header + those first ~45 lines** (a photo is fine). If `available=False`,
also run it for the defining repo/symbol you know is indexed, and paste whatever the fallback shows —
either way I need to see the real output shape to write the parser. (Optional second sample if easy:
`python dump_callgraph.py publishIngressEvent`.)

## Step 3 — re-run the caller-completeness probe
Make sure the webapp is up (streaming on is fine), then:
```
python probe_whocalls.py --n 5
```
We already know both phrasings hit the campaign-core caller 5/5 with a line. **This time I care about
the COUNT** — whether the "enumerate EVERY caller" prompt tweak pushed the `explicit` caller count up
and steadier (last run it was 2/5/2/6/6). If your probe prints a per-run caller count, send it; if not,
just eyeball the `explicit` answers and tell me roughly how many distinct callers each of the 5 listed.

## Send back
```
Step 2:  dump_callgraph IngressService   [ header line + first ~45 lines of raw output ]
Step 3:  probe explicit caller counts    [ e.g. 5/6/5/6/6 — is it fuller & steadier than 2/5/2/6/6? ]
```

## What we'll do with it
- The raw output from Step 2 lets me write a parser that pulls the complete caller set (repo · symbol ·
  file:line) out of CodeGraph deterministically, then inject it so the Q&A answer + diagram **never drop
  a caller** — the "authoritative complete list" a leadership audience can trust.
- Step 3 tells us how much the prompt tweak alone already bought us (maybe enough for the demo on its own).
