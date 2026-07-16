# RUNBOOK 35 (INTERNAL Codex / you) — verify Phase C: inline `impact` + `coverage` views

> **Pull `master` first, restart both services** (reloads agent/tool code). This is the
> **external Codex's Phase C build** (spec: `docs/specs/phase-c-inline-views.md`), reviewed and
> committed on top of the RUNBOOK-34 fixes. Two new tools now render **inline in the answer** the
> same way `show_arch` does:
> - `show_impact(repo)` — dependency blast-radius for a repo ("改 X 会连累谁 / who depends on X")
> - `show_coverage(kind, value)` — the 392-repo estate overview, optionally filtered
>
> This same commit also **strips the stray `<!-- architecture diagram rendered inline: … -->`
> comment** the model used to leak into its answer text (your figure-3 finding). Verify that too.

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
Sanity: `python -m unittest discover -s . -p "test_*.py"` should be **OK (89 tests)**.

## Step 2 — coverage (do this first, it never depends on the dep-graph)
On :8765 ask: **"给我看看所有 SMS 的仓库"**
- The answer should embed an **inline estate overview filtered to SMS** (the coverage grid, chrome
  stripped) — not a link, not a page you open.
- **Fits:** no horizontal scroll; the iframe auto-heights (no vertical scroll bar inside it).
- Pick a **real repo id** you can see in that grid — you'll use it in Step 3.

## Step 3 — impact (dependency blast-radius)
Ask: **"改 mc-hk-hase-api-tracking-core 会连累谁？"**
(If it answers `unknown repo`, that repo isn't in this box's index — re-ask with the real repo id
you picked in Step 2, e.g. **"改 <repo> 会连累谁？"**.)
- The answer should embed an **inline dependency diagram for that repo**, plus a text line with the
  **下游 N / 上游 M** counts.
- Below the diagram, the impact panel should show **受影响仓库** by relation (下游依赖 / 上游依赖).
- **Survives refresh:** hard-refresh (Ctrl+F5) and reopen that session (left list) — the diagram +
  panel should come back (persistence rides `views`, same as Phase B).

## Step 4 — the stray-comment fix (your figure-3 issue)
Ask something that triggers `show_arch`, e.g. **"CSL 出问题了，影响什么？"**
- The diagram renders inline as before.
- **In the answer TEXT there must be NO** `<!-- architecture diagram rendered inline: … -->` line
  (nor "图已插入" / "diagram shown above"). It's now stripped server-side + discouraged in the prompt.
- Note: only **new** answers are cleaned. An **old** session captured before this commit still has
  the comment baked into its stored text — that's expected; judge the fix on a fresh question.

## Send back
```
Step 2 coverage   [ inline SMS grid? horizontal/vertical scroll? ]
Step 3 impact     [ inline dep diagram + 下游/上游 counts + panel? persists on refresh? which repo id you used ]
Step 4 no-comment [ any "<!-- … rendered inline … -->" left in a FRESH answer's text? ]
tests             [ 89 OK? ]
```

## Notes
- The chat (:8765) embeds each view from the retrieval service (:8848) via an iframe, so **:8848 must
  stay up**.
- Impact/use-case numbers are best-effort from local indexes; if a panel is missing but the diagram
  shows, say so — the diagram is the must-have, the panel is the bonus. `show_impact` is defensive:
  a missing index returns the diagram-only view instead of crashing.
- Reference for how the pattern works end-to-end: `docs/specs/phase-c-inline-views.md`.
