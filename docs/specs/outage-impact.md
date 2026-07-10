# Spec (EXTERNAL Codex) — outage impact: failed channel/vendor/branch → affected use-cases + repos

> **Who builds: EXTERNAL Codex.** Verify on the box via a follow-up RUNBOOK. The messaging team's
> 25-min incident-response need: a delivery branch/vendor goes down → **which use-cases + repos +
> components are affected**, fast, cited. This is the **inverse** of `impact_report`, anchored on a
> channel/vendor/branch. Background + data shapes: `RUNBOOK-9` results, and the memory
> `mdc-messaging-architecture`. **Do not touch the Q&A app.** stdlib-only, read-only, writes only
> gitignored `index/` artifacts, no hardcoded internal vendor list (discover from names).

Key facts we build on (from discovery):
- **Channel + vendor are encoded in repo names** — delivery jobs `*-<vendor>-<channel>-deli-job`,
  outbound APIs `<vendor>-outbound-api`. These names are in the **full dep graph (all ~390)**, so
  the topology is derivable **now, without the full mirror**.
- Routing table `tbl_event_router_usecase_topic.snapshot.csv` = `use_case_id → topic_name`; the
  **channel is a token in the topic name** (e.g. `..._svc_rt_hr_sms`), not a column.
- **Breakpoint:** no `topic → delivery-job` message edge yet (message map is pilot/tracking-only),
  and HRN topic names ≠ `q_` queue names. Bridge with **shared seg/mode/channel tokens**.

## Building block 1 — topic → use-case reverse index
Invert the snapshot into `topic_name → [use_case_id,…]`. Add to `retriever/messages.py` (or a small
`retriever/usecase_index.py`): `use_cases_for_topic(topic)` and `use_cases_for_channel(channel)`
(the latter = union over topics whose name contains the channel token). Missing snapshot → empty,
never crash. Every result carries the snapshot row citation (`…snapshot.csv:<line>`).

## Building block 2 — `make_delivery_topology.py` → `index/delivery_topology.json`
Stdlib, read-only over `recon_out/internal_edges.csv` (repo names) + optional `index/repo_tags.json`.
**Discover structurally — do NOT hardcode vendor names:**
- Delivery jobs: regex `^(?P<prefix>.+?)-(?P<vendor>[a-z0-9]+)-(?P<channel>sms|mms|email|letter|whatsapp|wechat|push)-deli-job$`
  → record `{repo, channel, vendor, name_tokens}`. (channel list is the only fixed vocabulary; vendor
  is whatever token precedes the channel.)
- Outbound APIs: regex `-(?P<vendor>[a-z0-9-]+)-outbound-api$` → `{repo, vendor}`.
- Group into `{ "<channel>": { "<vendor>": { "delivery_jobs":[…], "outbound_apis":[…] } } }`, plus a
  flat `by_repo` index. Merge an optional hand-curated `index/delivery_topology.override.json` on top
  (for vendor↔outbound-api links or topic patterns names don't reveal).
- Print a coverage table: #channels, #vendors, #delivery-jobs, #outbound-apis matched, and any
  `*-deli-job` / `*-outbound-api` repos that did NOT parse (so we see gaps).

## Building block 3 — topic ↔ delivery-job bridge (two confidence tiers)
`resolve_topics_for(target)` where target is `channel=<c>` | `vendor=<v>` | `repo=<delivery-job>`:
- **Channel tier (high confidence, works now):** topics whose name contains the channel token →
  from the routing table. Cited by snapshot rows.
- **Vendor/branch tier (heuristic until full message map):** from `delivery_topology.json` get the
  vendor's delivery jobs → match topics by **shared seg/mode/channel tokens** (normalize `_`↔`-`,
  match on the job's `<seg>-<mode>-…-<channel>` vs the topic's `…_<seg>_<mode>_<channel>`). If the
  full `message_edges.csv` has a real `topic→delivery-job` edge, prefer it and mark source `message-map`.
- Each resolved topic tags its **source** = `channel-token` | `token-heuristic` | `message-map`.

## Building block 4 — `outage_report.py` + `/outage-impact`
CLI `python outage_report.py channel:sms | vendor:sinch | repo:<delivery-job>` and endpoint
`GET /outage-impact?channel=|vendor=|repo=` on `retrieval_service.py` (additive). Compose:
1. Resolve **affected topics** (block 3) + their **source/confidence**.
2. **Affected use-cases** = reverse index over those topics (count + list, cited to snapshot rows).
3. **Affected repos/components** = the delivery job(s) + their vendor's outbound-api(s) +
   `graph.impact()` dependency closure (so upstream libs / decision / ingress in the path show up).
4. **Channel(s)** involved; **confidence banner**: channel-level = solid; vendor-level = heuristic
   until the full message map lands (full mirror).
5. Render markdown `OUTAGE_IMPACT_<target>.md` + JSON, every claim cited.

## Building block 5 — surface it in the demo UI
Add an **「故障影响」** tab to `static/impact.html`: pick channel/vendor (or type a delivery-job
repo) → calls `/outage-impact` → shows **# affected use-cases**, the use-case list, affected
repos/components, and the confidence banner. (This is the "投屏救火演示".)

## Tests (fixtures, like existing)
- topology parse: a `mc-x-svc-bat-sinch-sms-deli-job` fixture → channel `sms`, vendor `sinch`.
- reverse index: a fixture snapshot topic `..._sms` → its use-cases.
- outage `channel:sms` → the right use-cases; `vendor:sinch` → heuristic-tagged topics + use-cases.
- `/outage-impact` returns same shape as CLI; unknown target → clean error.

## Honesty / limits (put in the report + PLAN)
Channel-level outage impact is solid from data we have today. **Vendor-level precision** (Sinch vs
CSL specifically) is heuristic until the **full message map** (needs the full-mirror / CodeGraph
resource ask) provides real `topic→delivery-job` edges. The query itself is instant → the 25-min
SLA is easily met once indexed.
