# Phase C spec — inline `impact` and `coverage` views in the Q&A (for EXTERNAL Codex)

## Goal (read this first)
We are turning the Q&A assistant (`webapp`, on :8765) into the **single front door**: a non-technical
user only talks to the assistant, and when their question maps to one of the standalone views, the
assistant renders that view **inline in its answer** (an embedded, highlighted diagram/table) — the
user never opens a page or clicks anything. This already works for the **architecture map** (the
`show_arch` tool). Your job: apply the exact same, already-proven pattern to **`impact.html`**
(dependency blast-radius) and **`coverage.html`** (the 392-repo overview), and add embed modes to
those two pages.

**Do NOT delete the standalone pages.** They stay; you are making them (a) embeddable and (b)
reachable through a tool the assistant calls.

## The proven pattern (reference: `show_arch`, already merged)
1. A tool in `webapp/tools.py` returns a **view directive**: `{ok, url, summary, impact?}` where
   `url` is a relative embed URL like `/arch.html?embed=1&highlight=vendor:sinch`.
2. `webapp/agent.py` — when that tool runs, it emits a stream event `{"type":"view","view":<result>}`
   and appends the directive to the `done` event's `views` list.
3. `webapp/server.py` + `webapp/session_store.py` persist `views` on the assistant message.
4. `webapp/static/index.html` — `renderInlineView(container, view)` renders the directive as an inline
   `<iframe src=RETRIEVAL_BASE+view.url>` plus a compact impact panel from `view.impact`.
   **This function is GENERIC — it already renders ANY view directive. You do not need to change the
   frontend for new views**, as long as your tool returns `{ok:true, url, summary, impact?}` with the
   same shape.
5. The embedded page reads `?embed=1` to strip its chrome to just the content, and `postMessage`s its
   height (`{type:"arch-embed-height", height}`) so the iframe auto-fits. (The frontend listener keys
   on `arch-embed-height` for all embeds — reuse that exact message type; don't invent a new one.)

`view.impact` (optional, rendered as chips) has this shape — reuse it verbatim so the panel renders:
```json
{"confidence":"high","use_cases":{"count":3,"items":["UC-A","UC-B"]},
 "repos":{"count":24,"by_relation":{"delivery-job":3,"dependency-downstream":20},"sample":["repo-a","repo-b"]}}
```

Look at these files as your template before starting: `retriever/arch_focus.py`, the `show_arch` branch
in `webapp/tools.py`, `static/arch.html` (search `embed`, `postEmbedHeight`, `applyDeepLink`).

---

## Task 1 — `show_impact(repo)` → inline dependency blast-radius

**1a. Embed mode for `static/impact.html`.** It already deep-links via `?target=<repo>` (see
`impact.html` ~line 1038, it auto-runs the impact report). Add `?embed=1` that:
- adds `body.embed`, hides the page header/nav/hero and any side chrome (mirror arch.html's
  `body.embed` CSS), leaving just the impact result;
- removes any fixed min-width so it fits the iframe width (no horizontal scroll);
- posts height to the parent: `parent.postMessage({type:"arch-embed-height", height:<contentHeight>}, "*")`
  after render and on resize (copy `postEmbedHeight` from arch.html verbatim).

**1b. Tool** — add to `TOOLS` in `webapp/tools.py` (verbatim):
```python
_schema("show_impact",
        "Render the dependency blast-radius INLINE in your answer for a repo the user wants to change "
        "or is worried about. Call this whenever the user asks 'what breaks if I change X', 'who depends "
        "on X', 'is X risky to touch', '改 X 会连累谁' (X = a repo name). `repo` is the repo id. The user "
        "SEES the impact inline; also summarise the downstream/upstream counts in text.",
        {"repo": {"type": "string"}}, ["repo"]),
```
**1c. Dispatch** — in `dispatch()` add (verbatim), reusing the existing `graph`/`impact_report`:
```python
    if name == "show_impact":
        repo = (a.get("repo") or "").strip()
        if not repo or repo not in graph.known_repos():
            return {"ok": False, "error": f"unknown repo: {repo}", "hint": "use an exact repo id"}
        dep = graph.impact(repo, transitive=True)
        down, up = dep["depends_on"], dep["depended_on_by"]
        return {
            "ok": True, "view": "impact",
            "url": f"/impact.html?embed=1&target={repo}",
            "summary": f"已在依赖图上展开 {repo} 的影响：下游 {len(down)} 个、上游 {len(up)} 个仓库。",
            "impact": {"repos": {"count": len(down) + len(up),
                                 "by_relation": {"dependency-downstream": len(down), "dependency-upstream": len(up)},
                                 "sample": sorted(down)[:6]}},
        }
```
(`graph.known_repos()` and `graph.impact` already exist and are imported in tools.py.)

**1d. Agent** — in `webapp/agent.py`, the block that emits the `view` event currently checks
`name == "show_arch"`. Widen it to also fire for `show_impact` (and `show_coverage`). Change:
```python
            if name == "show_arch" and isinstance(result, dict) and result.get("ok"):
```
to:
```python
            if name in ("show_arch", "show_impact", "show_coverage") and isinstance(result, dict) and result.get("ok"):
```
Nothing else in agent.py changes (views already ride `done` + persist).

**1e. Prompt** — in `prompts/qa-system-prompt.md`, in the "Inline architecture view" section, append
this paragraph (verbatim):
> **Dependency impact:** when the user asks what breaks if they change a repo, or who depends on it
> ("改 mc-hk-… 会连累谁", "who depends on X", "is X safe to touch"), call `show_impact(repo)` so the
> blast-radius diagram appears inline. Always also state the downstream/upstream counts in text.

## Task 2 — `show_coverage(kind, value)` → inline 392-repo overview

**2a. Embed mode for `static/coverage.html`** — same `?embed=1` treatment (strip chrome, fit width,
postMessage height). Add a filter param it honours, e.g. `?channel=sms` or `?q=tracking`, that
pre-filters the overview to the matching repos (wire it to coverage.html's existing search/filter).

**2b. Tool** (verbatim):
```python
_schema("show_coverage",
        "Render the 392-repo estate overview INLINE, optionally filtered. Call this when the user asks "
        "to see the repos on a channel or matching a keyword ('show me the SMS repos', '有哪些 tracking "
        "仓库', 'what does the estate look like'). `kind` is 'channel' or 'query'; `value` is the channel "
        "(sms/email/…) or a search keyword.",
        {"kind": {"type": "string"}, "value": {"type": "string"}}, ["kind"]),
```
**2c. Dispatch** (verbatim):
```python
    if name == "show_coverage":
        kind = (a.get("kind") or "").strip().lower()
        value = (a.get("value") or "").strip()
        param = ("channel=" + value) if kind == "channel" and value else (("q=" + value) if value else "")
        return {"ok": True, "view": "coverage",
                "url": "/coverage.html?embed=1" + ("&" + param if param else ""),
                "summary": ("仓库全景" + (f"（筛选：{kind}:{value}）" if value else "（全量 392）"))}
```
**2d. Prompt** — append to the same prompt section (verbatim):
> **Estate overview:** when the user asks to see which repos exist on a channel or matching a keyword
> ("有哪些 SMS 仓库", "show the tracking repos"), call `show_coverage(kind, value)`.

## Acceptance
1. `python -m unittest discover -s . -p "test_*.py"` still green.
2. On :8765 (with :8848 up), ask **"改 mc-hk-hase-api-tracking-core 会连累谁？"** → the answer has an
   **inline impact diagram** for that repo + downstream/upstream counts, and refreshing the page keeps
   the diagram (persistence already works via `views`).
3. Ask **"给我看看所有 SMS 的仓库"** → inline coverage overview filtered to SMS.
4. Each embed fits with **no horizontal scroll** and auto-heights (no vertical scroll).
5. `git grep -n "arch-embed-height"` shows impact.html + coverage.html posting the SAME message type.

## Guardrails
- Keep `webapp/llm.py` untouched (merge-sensitive facade). Only touch `tools.py`, `agent.py` (the one
  line above), the two HTML pages, and the prompt.
- The impact/use-case data is best-effort — if an index file is missing, return `{ok:true}` with just
  `url`+`summary` (the diagram still renders); never crash the tool.
- Don't add a new stream event type or a new frontend renderer — reuse `view` + `renderInlineView`.
