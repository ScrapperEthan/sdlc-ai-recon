# RUNBOOK 34 (INTERNAL Codex / you) — verify the three inline-view fixes

> **Pull `master` first, restart the webapp** (reloads agent/tool/session code). Fixes from your Phase-B
> testing: (a) the inline diagram **vanished on refresh**, (b) it **didn't fit / needed scrolling**, and
> (c) you **couldn't see which repos / use-cases** were affected. All three are in now.

## Step 1 — pull + restart
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

## Step 2 — ask, then check all three fixes on one answer
On :8765 ask: **"Sinch 出问题了，严重吗？影响哪些？"**

- **(c) impact shown:** below the inline diagram there should now be a compact panel —
  **受影响用例 N**（chips）and **受影响仓库 M**（by relation: 投递任务/供应商 API/下游依赖…）plus a few
  repo names. So you can see *which* use-cases/repos are hit without opening a page.
- **(b) fits:** the diagram should fit the answer width with **no horizontal scroll**, and the iframe
  height should match the diagram (**no vertical scroll bar** inside it).
- **(a) survives refresh:** now **hard-refresh the page (Ctrl+F5)** and reopen that session (left list).
  The answer should come back **with the diagram + impact panel still there** (previously it disappeared).

Also try **"短信 SMS 发不出去了，影响什么？"** → whole-SMS-lane diagram + its impact panel.

## Send back
```
(c) impact panel   [ screenshot: use-cases + repos-by-relation under the diagram? ]
(b) fit            [ any horizontal/vertical scroll inside the diagram? ]
(a) after refresh  [ does the diagram+panel persist when you reopen the session? ]
```

## Notes
- The chat (:8765) embeds the diagram from the retrieval service (:8848) via an iframe, so **:8848 must
  stay up**.
- Impact numbers are best-effort from the local indexes; if a panel is missing but the diagram shows,
  say so — the diagram is the must-have, the panel is the bonus.
- Next (handed to EXTERNAL Codex as a spec, `docs/specs/phase-c-inline-views.md`): the same inline
  treatment for `impact.html` ("改 X 会连累谁") and `coverage.html` ("给我看 SMS 的仓库").
