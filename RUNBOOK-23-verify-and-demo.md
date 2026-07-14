# RUNBOOK 23 (INTERNAL Codex) — verify both layers end-to-end + capture the demo

> **Who runs: INTERNAL Codex on the box.** **Pull `master` first.** Read-only / no writes to tracked
> files (just runs servers + queries). Goal: prove the two layers now work over the **full 390-repo
> estate** and grab screenshots for leadership — (A) the **function-call** layer (CodeGraph + routing),
> (B) the **dependency/channel** layer (impact / outage / arch UI). **Don't push — relay the screenshots
> + the small text blocks below.**
>
> Substitute the extract path `C:\Users\45509915\Downloads\HASE_MDC` if the box differs. Where a command
> says `<repo>`, pick any real repo from `index/repo_tags.json` (examples given are known hubs).

## Part A — function-call layer (CodeGraph + routing)

### A1. Is the build complete? Summarize the manifest
```
python -c "import json;m=json.load(open('index/codegraph_build.json',encoding='utf-8'));b=m.get('bundles',[]);ok=[x for x in b if x.get('returncode')==0];print('bundles built ok:',len(ok),'/',len(b));[print(' FAIL',x['bundle'],x.get('error') or x.get('returncode')) for x in b if x.get('returncode') not in (0,None)];[print(' skipped',x['bundle'],x.get('skipped')) for x in b if x.get('skipped')]"
```
Report: how many bundles built ok / total, and any failed or skipped.

### A2. Routing resolves to the right index
```
python cli.py unified-impact IngressService --bundle misc-ingress-to-lys
python cli.py unified-impact mc-hk-hase-api-tracking-core
```
Confirm in the JSON: `callers.available: true`, `returncode: 0`, and a real `bundle_root` (the second
routes by the repo's own bundle tag — no `--bundle` needed).

## Part B — dependency/channel layer (the refreshed 390 data)
Start the demo service (serves the UI + JSON on **:8848**):
```
set RETRIEVAL_PORT=8848
python retrieval_service.py
```
Leave it running; open a browser on the box.

### B1. Architecture map — `http://127.0.0.1:8848/arch.html`
- Confirm most repo-backed nodes now show a **repo-count badge** (SMS 投递任务 should be the big one,
  ~60+ repos; WhatsApp / Email / Push / Letter bound too).
- Click **SMS 投递任务** → side panel lists its bound repos; click **Sinch** (出站 API) → its repo +
  a **查故障影响** button.
- Click **查故障影响** on Sinch/SMS → the outage panel shows affected use-cases + affected-repo set with
  **relation chips** (`channel-owner` / `serves-channel` / `delivery-job`).
- Sanity: **HARO / CN-Gateway** nodes stay grey (known — they need the hand override, separate task).
- 📸 **Screenshot** the arch page + one opened node panel.

### B2. Impact / outage — `http://127.0.0.1:8848/impact.html`
- Enter a **channel** target `sms` and a **vendor** target `sinch`; confirm the affected-repo set now
  spans the full estate and the relation breakdown (`by_relation`) renders.
- Enter a **repo** target `<repo>` (e.g. `mc-hk-hase-api-tracking-core`) → up/down dependency impact.
- 📸 **Screenshot** one outage result (with the affected-repo count + chips).

### B3. JSON spot-checks (paste the numbers)
```
curl "http://127.0.0.1:8848/outage-impact?channel=sms"
curl "http://127.0.0.1:8848/outage-impact?vendor=sinch"
curl "http://127.0.0.1:8848/repos?channel=sms&mode=realtime"
```
Report the affected-repo `count` (and `by_relation`) for each outage, and the `/repos` count.

## Part C (optional) — Q&A app answers from the real call graph
```
python -m webapp.server
```
Open :8765, ask a deep question scoped to a **built** bundle, e.g. *"谁调用了 IngressService，跨 repo 的调用链是什么？"*
Confirm the answer uses **cross-repo callers** (real call graph), not lexical grep, with citations.
📸 Screenshot the answer.

## Send back
```
A1 manifest:   [ bundles built ok / total; any fail/skip ]
A2 routing:    [ unified-impact: callers.available / returncode / bundle_root for both seeds ]
B1 arch:       [ screenshot; SMS node repo-count; Sinch outage panel renders? ]
B2 impact:     [ screenshot; sms/sinch affected-repo count; repo-target up/down works? ]
B3 JSON:       [ outage sms count + by_relation; outage sinch count; /repos count ]
C Q&A:         [ (optional) screenshot; used real call graph? ]
Surprises:     [ ... ]
```

## What this establishes
Green = **both halves of "Step 1" are live over the full estate**: dependency + channel + outage impact
(cited, full 390) AND function-call "who-calls/trace across repos" (routed to the right CodeGraph index).
That's the demo-able baseline. Then the next build is the **message map** (connect the ~214 messaging-only
repos to channels) — the flagship impact-notification enabler. Known small follow-ups: `arch_map.override.json`
for HARO/CN-Gateway; regenerate `bundles.json` from the full graph after the CodeGraph build settles.
