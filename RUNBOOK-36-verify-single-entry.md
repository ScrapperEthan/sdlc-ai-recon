# RUNBOOK 36 (INTERNAL Codex / you) — verify the single-entry merge (+ the two fixes)

> **Pull `master`, then launch via the new single entry.** The chat now reverse-proxies the
> arch/impact/coverage pages + their data, so **you only ever open one port (:8765)** — the inline
> views load same-origin, no `:8848` to reach. This pull also carries two fixes verified locally:
> `show_impact` dependency direction (was inverted) and the arch same-column edge (was a tangle).

## Step 1 — pull + launch (one script)
```
git pull
```
Set your usual env (whatever you already export), then:
```
start.bat
```
`start.bat` starts the retrieval service on loopback (`127.0.0.1:8848`) and the chat on `:8765`,
and you open **only** `http://127.0.0.1:8765`. (Manual equivalent still works: run
`python retrieval_service.py` and `python -m webapp.server` in two terminals — the chat proxies to
`RETRIEVAL_UPSTREAM_URL`, default `http://127.0.0.1:8848`.)

Sanity: `python -m unittest discover -s . -p "test_*.py"` → **OK (95 tests)**.

## Step 2 — single entry works
- `http://127.0.0.1:8765/` → the chat loads.
- `http://127.0.0.1:8765/health` → `{"ok": true, "webapp": "ok", "retrieval": { … }}` (one health call covers both).
- Open `http://127.0.0.1:8765/arch.html` directly → the arch map renders **through :8765** (proxied).
  Same for `/impact.html` and `/coverage.html`.

## Step 3 — inline views load same-origin (no :8848)
Ask **"Sinch 出问题了，影响什么？"** → the highlighted diagram appears inline as before. Confirm the
embedded iframe's `src` is now **`http://127.0.0.1:8765/arch.html?...`** (same origin), not `:8848`.

## Step 4 — the show_impact direction FIX
Ask **"改 mc-hk-hase-api-tracking-core 会连累谁？"** → the inline impact summary + panel must now read
**下游（受影响）345 / 上游（依赖）10** (previously it was inverted: 下游 10 / 上游 345). Downstream =
the consumers that break if you change it; upstream = what it needs.

## Step 5 — the arch same-column edge FIX
On `http://127.0.0.1:8765/arch.html`, the **decision-topics → decision-job** line (both in the
决策 column) should now be a **clean vertical connector**, not a curve that loops back on itself.
Every other edge is unchanged.

## Step 6 — status numbers no longer confusing
Hover the index-status pill in the chat header → the tooltip should now read
**`镜像扫描 456 仓库 · MDC 产品 392 仓库`** (previously just "456 repos tracked", which conflated the
mirror scan with the product estate).

## Send back
```
Step 2  [ :8765/ , :8765/health , :8765/arch.html all OK? ]
Step 3  [ inline arch iframe src = :8765 (same-origin)? ]
Step 4  [ 改 tracking-core → 下游 345 / 上游 10 now? ]
Step 5  [ decision-topics→job = clean vertical line? ]
Step 6  [ tooltip shows 镜像扫描 456 · MDC 产品 392? ]
tests   [ 95 OK? ]
```

## Notes
- If an inline view shows a **502 / "retrieval service unavailable"**, the retrieval service isn't
  up — `start.bat` starts it; the manual path needs `python retrieval_service.py` running.
- Nothing about the two-terminal launch breaks: the proxy target defaults to the same `:8848` you
  already run. The win is you can now share/open a single URL and the views are same-origin.
