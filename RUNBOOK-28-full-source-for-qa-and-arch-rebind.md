# RUNBOOK 28 (INTERNAL Codex / you) — point Q&A at the FULL source + rebind the arch map

> **Who runs: you or INTERNAL Codex on the box.** **Pull `master` first.** No LLM quota for the setup
> steps (the Q&A verify at the end does use the app). Two fixes: (A) the Q&A can only read the ~16-repo
> `mirror/`, so cross-repo files (e.g. `SendCampaignEventService.java` in `…-api-campaign-core`) have no
> readable source → no line numbers; (B) the arch map needs regenerating so the vendor/terminal nodes
> now bind to our repos. Both are config/rerun only.

## Why the Q&A misses full source (the root cause)
The graph / message / CodeGraph tools already span all 392 repos. But `search_code` / `read_file` read
**`config.MIRROR`**, which on the box is the small `mirror/` (~16 repos). The full source is the
`HASE_MDC` extract. So the agent finds a cross-repo caller via CodeGraph but can't open the file to cite a
line. Fix: run the servers with **`SDLC_MIRROR` pointed at the full extract** — no copy, no symlink.

## Step 1 — regenerate the arch map (vendor/terminal nodes now bind)
```
python make_arch_map.py
```
(Reads the existing `index/delivery_topology.json` + `index/repo_tags.json` — seconds.) Expect the
coverage line to jump, e.g. `nodes bound / empty: ~26 / ~16`.

## Step 2 — start the servers pointed at the FULL extract
```
set SDLC_MIRROR=C:\Users\45509915\Downloads\HASE_MDC
set RETRIEVAL_PORT=8848
python retrieval_service.py
```
new terminal (same `SDLC_MIRROR`):
```
set SDLC_MIRROR=C:\Users\45509915\Downloads\HASE_MDC
python -m webapp.server
```
> `SDLC_MIRROR` makes `search_code`/`read_file` read all 392 repos' source. Nothing else changes
> (CodeGraph indexes, graph, tags are unaffected). To make it permanent, set `SDLC_MIRROR` as a user
> environment variable so every launch picks it up.

## Step 3 — verify
- **arch** `http://127.0.0.1:8848/arch.html` → click **Sinch** (供应商) → side panel now lists the sinch
  SMS repos under "对接该终端/供应商的我方仓库", with a **查故障影响** button; click **APNs / FCM** → push
  repos + a channel-outage button. Bound count in the stat band is up (~26/42).
- **Q&A** `:8765` → ask again **"谁调用了 IngressService？"** → this time it should cite the caller in
  `…-api-campaign-core` **with a file:line** (source is now readable), not "该文件不在可读 mirror 中".

## Send back
```
Step 1 arch:  [ nodes bound / empty ]
Step 3 arch:  [ Sinch / APNs-FCM show repos + outage button? screenshot ]
Step 3 Q&A:   [ does the campaign-core caller now have a file:line citation? ]
```

## What this establishes
The Q&A reads the whole estate's source (full citations), and every arch node that touches a channel —
including the third-party terminals — now maps to the real repos that feed it and is a one-click outage
entry point. That closes both gaps from the last review.
