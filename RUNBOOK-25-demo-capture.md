# RUNBOOK 25 (INTERNAL Codex) — capture the demo pack (arch map + Q&A who-calls)

> **Who runs: INTERNAL Codex on the box.** **Pull `master` first** (arch.html demo polish is in it).
> Read-only; just runs the two servers and takes screenshots. **Relay the 3 screenshots below** — they
> are the leadership demo pack. No writes, no push.

## 1. Architecture map — the "whole estate at a glance" shot
```
set RETRIEVAL_PORT=8848
python retrieval_service.py
```
Open `http://127.0.0.1:8848/arch.html`. The hero now has a **stat band** — confirm it shows:
- **全量系统仓库 = 392** (the whole estate)
- **管线节点已映射到仓库 = 13 / 42**
- **最大投递渠道 · SMS = 63 repos**
- **已覆盖投递渠道 = N**

And the diagram nodes are now **sized by repo count** (SMS 投递任务 is the biggest badge, small
channels smallest). 📸 **Screenshot the full hero + diagram.** Then click **SMS 投递任务** → side panel
lists 63 repos; click **Sinch → 查故障影响** → outage panel. 📸 **Screenshot one opened node.**

## 2. Q&A app — the "AI walks the real call graph" shot (the RUNBOOK-19 Step 5 we skipped)
```
python -m webapp.server
```
Open `http://127.0.0.1:8765`. Ask, scoped to a built bundle:
> **谁调用了 IngressService？跨 repo 的调用链是什么？**

Confirm the answer lists **real cross-repo callers** (e.g. the 13 callers in
`mc-hk-hase-api-campaign-core/.../SendCampaignEventService.java`) with **citations**, not a lexical/grep
guess. 📸 **Screenshot the answer.**

## 3. (already captured, keep for the pack) outage impact
From RUNBOOK-23 you already have SMS outage = 101 affected repos with relation chips. Keep that shot.

## Send back
The 3 screenshots (arch hero+diagram, one opened node, Q&A who-calls answer). One line each on whether it
rendered as described. That's the pack we show leadership: **read the whole estate (392 repos) → click any
pipeline node to its real repos → ask the AI who-calls across repos and get a cited call graph.**
