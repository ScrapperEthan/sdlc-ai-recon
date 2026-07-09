# Spec (EXTERNAL Codex) — impact-analysis demo UI (front-end over the existing service)

> **Who builds: EXTERNAL Codex** (working tree). Purpose: a **presentable demo page** to show
> non-technical stakeholders (leadership, coding teams) how we answer *"this use-case / repo /
> topic → which channel chain + which upstream/downstream services, with proof."* The **back-end
> already exists** (`retrieval_service.py` serves `/impact-report` and `/repos`); this is mostly a
> **front-end**. **Do not touch the Q&A app** (`webapp/*`). Self-contained, stdlib-served, read-only.

## What to build

### 1. Serve a static page from `retrieval_service.py`
Add a `GET /` route (currently 404) that serves a single self-contained HTML file
`static/impact.html` (new top-level `static/` dir; **not** `webapp/static/`). Also serve
`GET /static/impact.html` for convenience. Keep it additive — don't change existing routes.

### 2. `static/impact.html` — one self-contained page (inline CSS + JS, NO external assets)
Air-gapped/CSP-safe: no CDN, no external fonts/scripts. Chinese-primary labels (demo audience).

**Controls (top):**
- A target type selector: `仓库 (repo) / use-case / 主题 (topic)`.
- A text input for the target value + a **「分析影响」** button.
- (Optional second tab **「按标签浏览」**) filter inputs `渠道 / 模式 / 系统` → calls `/repos`.

**On analyze:** `fetch("/impact-report?target=" + encoded)` (prefix `use-case:` / `topic:` per the
selector) and render the JSON (shape from `impact_report.build_report`):
- **Target card**: `input`, `description` (glossary-expanded name), and **channels as colored
  badges** (SMS / PUSH(PN) / WhatsApp / Email / Letter / WeChat — distinct colors). For use-case,
  also show `matched_topics`.
- **Two columns**: **上游(依赖谁)** = `upstream[]`, **下游(谁受影响)** = `downstream[]`. Each row:
  repo name, a `direct/transitive` chip, and a small "出处" toggle showing `citations[]`.
- **异步链路** = `async_routes[]`: per `destination`, list producers / consumers + channels + citations.
- **风险提示** = `risk_callouts[]`: render `hub` callouts and the `honesty` note prominently
  (e.g. a yellow banner for the "dev/SCT snapshot, verify vs prod" note).
- **出处总数** = `citations.length`, with a collapsible full list.
- Handle empty gracefully: `downstream: none known`, `channels: unknown`, error JSON → a clean message.

**Design:** clean, presentable, works in a projector. Light theme is fine. Badges + cards + two
columns. No framework — plain JS + a `<style>` block.

## Acceptance / verify (internal Codex will do this on the box, RUNBOOK note)
- `python retrieval_service.py`, open `http://127.0.0.1:8848/` → the page loads.
- Enter a real repo → renders target + upstream/downstream + async routes + channel badges + citations.
- Enter a `use-case` and a `topic` → both render (use-case shows matched topic + the honesty banner).
- 「按标签浏览」 with `渠道=sms` → lists repos (from `/repos`).
- Q&A app still runs unchanged on its own port at the same time.

## Deliver
`static/impact.html` + the `GET /` (and `/static/`) route in `retrieval_service.py` + a one-line
README table entry. Tests: extend `tests/test_retrieval_service.py` so `GET /` returns 200 +
`text/html`. Keep everything additive; no new pip deps.
