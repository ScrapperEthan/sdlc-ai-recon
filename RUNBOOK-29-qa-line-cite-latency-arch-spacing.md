# RUNBOOK 29 (INTERNAL Codex / you) — land the QA line-citation fix, the anti-hang guard, and the arch spacing fix

> **Who runs: you or INTERNAL Codex on the box.** **Pull `master` first** (this runbook assumes the
> commits below are already pushed to `master`). No new setup, no LLM quota for steps 1–3; the Q&A
> re-verify at the end uses the app. This closes the three things RUNBOOK-28's return flagged:
> (A) general Q&A cited the caller at *file* level, not `file:line`; (B) a focused follow-up hung
> >100 s and needed a webapp restart; (C) on `arch.html` the SMS outbound nodes **CSL / 3HK / Sinch**
> overlapped. All three fixes are already in the repo — you just pull, restart, and verify.

## What changed in the repo (so you know what you're pulling)
- `prompts/qa-system-prompt.md` — a hard rule + worked example: any named caller/callee MUST be cited
  `repo/path/File.java:line`; if the call graph gives only a file, `search_code` the called method or
  `read_file` and pin the line **before answering** — never defer to a follow-up. *(Read fresh on every
  question — no restart needed, but pulling updates it.)*
- `retriever/unified_impact.py` — the tool result now carries a `citation_contract` string, so the model
  is reminded to pin the line at the exact moment it's holding a caller-without-line.
- `retriever/code.py` — `search_code` now has a wall-clock ceiling (`SDLC_SEARCH_DEADLINE`, default 20 s)
  on both the ripgrep call and the stdlib fallback. The stdlib fallback walks the WHOLE `HASE_MDC`
  extract; for a sparse pattern that could run for minutes → the likely stall. It now returns partial
  results instead of hanging.
- `static/arch_nodes.json` — re-spaced the whole diagram to ≥1.0 slot rows (uniform 12 px gaps).
  CSL/3HK/Sinch now sit on their own rows (1/2/3); the SMS topic/delivery nodes align to the middle;
  the **source adapters** and the **Redis/Postgres/OpenSearch** cluster (which also overlapped) are
  spaced too. Verified locally: 42/42 nodes render, 0 overlaps. *(Client-side layout only — a browser
  hard-refresh is enough, but restart retrieval anyway to pick up the `code.py` change.)*

## Step 0 — the big latency lever: make sure `ripgrep` is on PATH
The >100 s hang is most likely the stdlib search fallback walking all ~390 repos because `rg` wasn't
found. Check:
```
where rg
rg --version
```
- If it prints a path/version → good, retrieval uses ripgrep (fast); the deadline is just a backstop.
- If "not found" → the app silently falls back to a slow full-tree Python walk. Put `rg` on PATH (it's
  a single self-contained binary; if ops can't install it, at least keep `SDLC_SEARCH_DEADLINE` at its
  default so a search can't run longer than 20 s).

## Step 1 — pull
```
git pull            # fast-forward master to the commits described above
```

## Step 2 — restart BOTH services pointed at the FULL extract
Code changed in `retriever/*`, so the running processes must restart to reload it. Stop the current
retrieval (:8848) and webapp (:8765), then relaunch with the full-source env var — same as RUNBOOK-28:
```
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
set RETRIEVAL_PORT=8848
python retrieval_service.py
```
new terminal (same `SDLC_MIRROR`):
```
set SDLC_MIRROR=C:\Users\45589915\Downloads\HASE_MDC
python -m webapp.server
```
> ⚠ RUNBOOK-28 printed the path with a typo'd user (`45509915`). Use the one that actually worked on your
> box — from your last return that was `45589915`. To make it permanent, set `SDLC_MIRROR` as a user
> environment variable so every launch picks it up.

## Step 3 — verify the three fixes

**(C) arch spacing — quickest.** Open `http://127.0.0.1:8848/arch.html` and **hard-refresh**
(Ctrl+F5 — the old `arch_nodes.json` may be cached). In the **出站 API** column, CSL / 3HK / Sinch are
now three separate rows with clear gaps (no overlap); the **来源** adapters and the Redis/Postgres/
OpenSearch stores are also cleanly spaced. Screenshot it.

**(A) Q&A now pins the line.** On `:8765` re-ask **"谁调用了 IngressService？"**. The answer's
`## Evidence` must cite the caller in `mc-hk-hase-api-campaign-core` **with a line**, e.g.
`…/SendCampaignEventService.java:51` — in the FIRST answer, not "ask a follow-up for the line", not a
file-only reference. (Reference verification should stay green.)

**(B) no hang.** The focused follow-up that stalled last time should now return within the model's
normal time. If a single query is still slow: confirm `rg` is on PATH (Step 0); note that a stuck model
call self-clears at `LLM_TIMEOUT` (120 s) — **don't panic-restart before then** — and can be shortened
with `set LLM_TIMEOUT=60` if you prefer a faster failure over a slow-but-real answer.

## Step 4 (optional) — light up the two hand-mapped nodes (HARO / CN Gateway)
These two stay empty by design because their repo names don't reveal the channel. To bind them, create
`index/arch_map.override.json` with the REAL repo names (look them up on the box first), then rerun the
map. Find the candidates:
```
rg -l -i "haro"        %SDLC_MIRROR%      &  :: WhatsApp gateway repos
rg -l -i "wechat|weixin|cn-gateway" %SDLC_MIRROR%   :: WeChat / CN Gateway repos
```
Then:
```
index/arch_map.override.json
{
  "whatsapp-haro": { "repos": ["<real-haro-repo>"], "serves_channels": ["whatsapp"], "note": "bound by hand" },
  "wechat-gw":     { "repos": ["<real-cn-gateway-repo>"], "serves_channels": ["wechat"], "note": "bound by hand" }
}
```
```
python make_arch_map.py     # bound/empty should tick up by ~2
```
(Skip this if you're unsure of the repo names — leaving them honestly empty is fine.)

## Send back
```
Step 0:  rg on PATH?           [ yes / no ]
Step 3C: arch.html spacing     [ CSL/3HK/Sinch separated? screenshot ]
Step 3A: IngressService Q&A    [ does the campaign-core caller now show :line in the first answer? paste the Evidence bullet ]
Step 3B: focused follow-up     [ returned in normal time? or still slow? ]

Two sanity checks I still want (from RUNBOOK-28's numbers):
- Sinch outage reverse-lookup said 118 affected repos. Paste any 5 of them so we can confirm the
  fan-out is real and not an over-broad tag match.
- Paste the "Unbound nodes: …" line that make_arch_map.py prints, so we can confirm the 20 empty
  nodes are only infra / sources / topics / HARO / CN-Gateway (all expected).
```

## What this establishes
The assistant's ordinary answers now carry exact `file:line` citations (the retrieval-moat selling
point), a single slow search can no longer wedge the app, and the architecture map is
demo-clean — no overlapping nodes in any column.
