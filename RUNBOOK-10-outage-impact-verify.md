# RUNBOOK 10 (INTERNAL Codex) — verify outage-impact on the real estate

> **Who runs: INTERNAL Codex on the box** (real `recon_out/`, `index/`, `mirror/`). Runs the code
> EXTERNAL Codex built from `docs/specs/outage-impact.md` (pull `master` first). **Read-only over
> the estate**; writes only generated `index/*.json` + `index/reports/`. Don't push — relay results.
> Background: memory `mdc-messaging-architecture`, `RUNBOOK-9` results.

## Task A — generate the real delivery topology (from all ~390 repo names)
```
python make_delivery_topology.py
```
Writes `index/delivery_topology.json` (gitignored) + prints a coverage table. **Relay (photo) the
table**: #channels, #vendors, #delivery_jobs, #outbound_apis, and any **unparsed** `*-deli-job` /
`*-outbound-api` (those are naming exceptions we may need to handle). Sanity-check: does it find the
channels (sms/mms/email/letter/whatsapp/wechat/push) and vendors (sinch/csl/htcl/lx/cm/pfp/…) we saw
in RUNBOOK-9?

## Task B — channel-level outage (should be SOLID today)
```
python outage_report.py channel:sms
python outage_report.py channel:whatsapp
```
Confirm each prints: a **confidence banner = 渠道级…可靠**, a list of **affected topics** (source
`channel-token`), an **affected use-cases** count + list, **affected repos/components**, and
citations to the snapshot rows. Spot-check one use-case against the snapshot (its `topic_name`
contains that channel token).

## Task C — vendor/branch-level outage (HEURISTIC until full message map)
```
python outage_report.py vendor:sinch
python outage_report.py repo:<a-real-sinch-sms-delivery-job>   # e.g. from RUNBOOK-9 Q4
```
Confirm: banner = 供应商/分支级…启发式; topics tagged `token-heuristic` (or `message-map` if any
real edge exists); affected use-cases + repos listed. Report **how many topics/use-cases** it finds
for `vendor:sinch` and whether the matches look right (the SMS use-cases from RUNBOOK-9 Q5, e.g.
`C9508`/`I0028`, should appear if their topic tokens match a Sinch job).

## Task D — endpoint + demo tab (both up with the Q&A app)
```
python retrieval_service.py      # terminal 1
python -m webapp.server          # terminal 2 (prove Q&A app still runs)
curl.exe -s "http://127.0.0.1:8848/outage-impact?channel=sms"
curl.exe -s "http://127.0.0.1:8848/outage-impact?vendor=sinch"
```
Then open `http://127.0.0.1:8848/` in a browser → the **「故障影响」** tab → pick channel `sms` /
vendor `sinch` → confirm it renders affected use-cases + repos + the confidence banner. (This is the
投屏救火演示.)

## Send back (paste this filled in)
```
Task A topology:   [ coverage table; any unparsed deli-job/outbound-api? ]
Task B channel:sms:[ #affected topics / #use-cases / #repos; banner = reliable? one spot-check ok? ]
Task C vendor:sinch:[ #topics/#use-cases; heuristic tag? do C9508/I0028-style SMS use-cases appear? ]
Task D endpoint+UI:[ both servers up? /outage-impact returns JSON? 故障影响 tab renders? ]
Surprises / errors:[ ... ]
```

## What this establishes
Task B green = **channel-level outage impact is real today** (the 25-min救火 demo works on real
data, cited). Task C shows the **vendor-level heuristic** and how much precision is still gated on
the full message map (the full-mirror / CodeGraph resource ask). Relay the numbers; we then tune the
topology override / decide whether to prioritise the full message-map build.
