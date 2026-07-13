# Spec (EXTERNAL Codex) — architecture map UI: the MDC pipeline diagram ↔ real repos

> **Who builds: EXTERNAL Codex** (working tree). Purpose: a **presentable, clickable** rendering of
> the team's real MDC notification pipeline where **every node maps to the real repo(s) behind it** —
> click a node → see its repos + jump to impact / outage analysis. Investor/leadership + onboarding +
> 投屏救火. The back-end already exists (`retrieval_service.py`: `/impact-report`, `/repos`,
> `/outage-impact`). This is mostly **front-end + a data-driven node→repo map**. **Do not touch the
> Q&A app** (`webapp/*`). Self-contained, stdlib-served, read-only, air-gapped/CSP-safe (no CDN).
> Node→repo mapping is **data-driven from a JSON the internal Codex fills on the box** — do NOT hardcode
> repo names in the HTML.

## Confirmed topology (bake this in — corrected 2026-07-13, see memory `mdc-messaging-architecture`)
`Ingress API → Decision Topics → Decision Job ──routes by use-case──►` per-channel `Topics → Delivery Job → Outbound → vendor → Client`:
- **Push** → Push Topics → Push Delivery Job → AWS SNS → APNs/FCM
- **SMS** → SMS Topics → SMS Delivery Job → **CSL / 3HK / Sinch** Outbound API → CSL SMSC / 3HK SMSC / Sinch
- **MMS** → MMS Topics → MMS Delivery Job → **3HK** Outbound API (HTTPS→3HK Gateway) → 3HK MMSC
- **Email** → Email Topics → Email Delivery Job → **PFP** Outbound API → ProofPoint
- **Letter** → Letter Topics → Letter Delivery Job → **HSBC ICCM / OTX**
- **WhatsApp** → WhatsApp Topics → WhatsApp Delivery Job → **HASE HARO** → WhatsApp  ⚠️ HARO is WhatsApp's
- **WeChat** → WeChat Topics → WeChat Delivery Job → **CN Gateway (Nginx) → Lease line** → WeChat  ⚠️ NOT HARO
Ingress fed by: DSPs (IB2B/EB2B via Kong), File Adapter (S3/Juniper), MQ Adapter, Kafka Adapter (MDC SDK).
Decision uses Redis (use-case/template), PostgreSQL (cust info), OpenSearch (records). Resilience: DR SQS / DR EKS.

## Building block 1 — node catalog + node→repo map (`index/arch_map.json`)
Define a **static node catalog** in the repo (part of the HTML or a `static/arch_nodes.json`): the ~30
diagram nodes with `{id, label, column, channel?, vendor?, role?}` where `role ∈ {ingress, decision,
topic, delivery-job, outbound-api, external}`. This is the drawing skeleton — safe to commit (no repo
names, no secrets).

The **node→repo binding** is generated on the box (gitignored) by a small stdlib script
`make_arch_map.py` → `index/arch_map.json`, resolving each node to real repos from data we already have:
- **delivery-job / outbound-api / topic nodes** ← `index/delivery_topology.json` (channel+vendor→repos)
  and `index/repo_tags.json` (repos whose `channel`/name matches the node's channel).
- **ingress / decision nodes** ← `repo_tags.json` by name/token (`*-ingress-*`, `*-decision-*`).
- Each node also carries the **`serves_channels`** rollup and a repo count.
- Print a coverage line: nodes bound / nodes empty (so we see gaps). Merge an optional hand
  `index/arch_map.override.json` for nodes names don't reveal (e.g. HARO, CN Gateway if they are repos).

`retrieval_service.py`: add `GET /arch-map` returning `index/arch_map.json` (or `{}` + a clear note if
absent), additive, like the other routes.

## Building block 2 — `static/arch.html` (faithful SVG, clickable)
One self-contained page (inline CSS/JS). Hand-author an **SVG that faithfully mirrors the real diagram**
layout (left→right columns: Sources → Ingress/Decision → per-channel Topics → Delivery Jobs → Outbound
APIs → Vendors/Client), using the node catalog. Chinese-primary labels. Each node is a `<g data-node-id>`.
- **On load:** `fetch("/arch-map")`; for each node show a small **badge with its repo count**, and grey
  out nodes with 0 bound repos (honest about coverage).
- **On node click:** open a side panel showing the node's **bound repos** (from arch_map), each linking to
  the existing impact page (`impact.html?target=<repo>`), plus the node's `serves_channels`.
  - For a **channel node** (e.g. SMS Topics) or **vendor node** (e.g. Sinch), also render a **「查故障影响」**
    button → `fetch("/outage-impact?channel=sms" | "?vendor=sinch")` and show affected use-cases count +
    confidence banner inline (reuse the outage rendering from `impact.html`).
- **Confidence honesty:** channel nodes = solid; vendor nodes = heuristic banner (until full message map).

## Building block 3 — wire it together
- Serve `GET /arch.html` (and `/static/arch.html`) from `retrieval_service.py`.
- Add a nav link between `impact.html` ("分析") and `arch.html` ("架构图") so it's one demo app.
- Keep everything additive; Q&A app untouched; no new pip deps.

## Building block 4 — keep ingestion re-runnable (so the map never goes stale)
`make_arch_map.py` must be a normal step in the refresh chain. Add it to `refresh.py` (after
`make_delivery_topology.py`) so one `python refresh.py` re-ingests: poms → edges → bundles → repo_tags →
enrich_repo_tags → delivery_topology → **arch_map**. (Scheduling it as a Windows Task / cron is a box-side
op note in the RUNBOOK, not code here.)

## Tests (stdlib fixtures, like existing)
- `make_arch_map`: a fixture `delivery_topology.json` + `repo_tags.json` → an SMS node binds the sms
  delivery-job(s); an unbound node → empty list; `serves_channels` rolled up. `Others` never a channel.
- `retrieval_service`: `GET /arch.html` → 200 `text/html`; `GET /arch-map` → 200 JSON (and clean note when
  the file is absent).

## Deliver
`static/arch.html`, static node catalog, `make_arch_map.py`, the `GET /arch.html` + `/arch-map` routes,
`refresh.py` wiring, README line, tests. Verify on the box via a follow-up RUNBOOK (fill arch_map from real
`delivery_topology.json`/`repo_tags.json`, open the page, click SMS/Sinch/WhatsApp nodes, confirm the
repos + outage panel render and that WhatsApp→HARO / WeChat→CN-Gateway are correct).

## Honesty / limits
Node→repo binding is only as complete as name/topology signals — **ingress/decision** and pure-infra
nodes (HARO, CN Gateway) may need the hand override or stay thin until the full mirror/message map.
Vendor-level outage stays heuristic until the message map. The diagram is the *documented* pipeline;
where a node has no repo, show it honestly rather than inventing one.
