# RUNBOOK 14 (INTERNAL Codex) — verify the architecture-map UI on the real estate

> **Who runs: INTERNAL Codex on the box** (real `index/delivery_topology.json`, `index/repo_tags.json`).
> Runs the code from `docs/specs/architecture-map-ui.md` (**pull `master` first**, HEAD `57d4b84`+).
> **Read-only over the estate**; writes only generated `index/arch_map.json` (+ optional hand
> `index/arch_map.override.json`). Don't push — relay results (photo). The node→repo binding does NOT
> exist until you generate it here.

## Task A — generate the node→repo map
```
python make_arch_map.py
```
Writes `index/arch_map.json` + prints `nodes bound / empty (of 42)` and the **unbound node list**.
**Relay (photo) that coverage line + the unbound list.** Expected: the delivery-job / outbound-api /
ingress / decision nodes bind from names; **topics** (Kafka, not repos) and **name-opaque infra**
(`whatsapp-haro` = HASE HARO, `wechat-gw` = CN Gateway) will likely be **empty** — that's honest, not a
bug. Sanity-check a couple: `python -c "import json;d=json.load(open('index/arch_map.json'));print({k:d['nodes'][k]['repo_count'] for k in ('sms-deli','email-deli','sms-sinch','whatsapp-deli','wechat-deli')})"`.

## Task B — bind the name-opaque nodes (only if real repos exist)
For the unbound infra nodes, check whether a real repo exists and, if so, add it to
`index/arch_map.override.json` (gitignored, box-local), then re-run Task A:
```
# is there a HARO / CN-gateway repo in the estate?
python cli.py search-code haro          # or grep repo_tags.json keys for haro / cn-gateway / wechat
```
Override shape (only for nodes names don't reveal):
```json
{ "whatsapp-haro": { "repos": ["<real-haro-repo>"], "serves_channels": ["whatsapp"], "note": "HARO=WhatsApp" },
  "wechat-gw":     { "repos": ["<real-cn-gateway-repo>"], "serves_channels": ["wechat"], "note": "CN Gateway=WeChat" } }
```
If **no** such repo exists (HARO/CN Gateway are pure infra, not in our repos), leave them empty and just
report that — the diagram shows them honestly greyed out.

## Task C — open the page (投屏 check)
```
python retrieval_service.py        # terminal 1
```
Open `http://127.0.0.1:8848/arch.html`. Confirm:
- The pipeline renders; nodes show a **repo-count badge**; empty nodes are greyed.
- Click **SMS 投递任务** → side panel lists the real sms delivery-job repos, each linking to
  `impact.html?target=<repo>`; the **「查故障影响」** button returns affected use-cases + a solid banner.
- Click **Sinch** (outbound-api) → its outbound repo + a heuristic-confidence outage panel.
- **Critical topology check:** the **WhatsApp** branch points at **HASE HARO**, and the **WeChat**
  branch points at **CN Gateway** (NOT HARO). Confirm the labels/edges match.
- The nav switches between 分析 (impact.html) and 架构图 (arch.html); both load; Q&A app still runs.

## Task D — tests + the full refresh chain
```
python -m unittest discover -s tests -p "test_*.py"     # elevate if the sandbox blocks temp writes -> expect 27 pass
python refresh.py                                         # optional: confirms make_arch_map is wired into ingestion
```

## Send back (paste this filled in)
```
Task A map:       [ nodes bound/empty (of 42); the unbound list; the 5 spot-check repo_counts ]
Task B override:  [ did HARO / CN-gateway repos exist? what you bound, or "infra, left empty" ]
Task C page:      [ renders? SMS/Sinch panels ok? WhatsApp->HARO & WeChat->CN-Gateway correct? nav ok? Q&A app still up? ]
Task D tests:     [ 27 pass? refresh.py chain ok? ]
Surprises/errors: [ ... ]
```

## What this establishes
Green = a **clickable architecture map bound to real repos** on top of the real estate: pick any pipeline
node → its repos + impact + channel/vendor outage, cited — the onboarding/投屏 artifact — with the
corrected HARO/WeChat topology. Unbound infra nodes are shown honestly. Next: wire `serves_channels`
into `outage_report` so the outage panels return owners+servers (now unblocked — RUNBOOK-13 is green).
```
