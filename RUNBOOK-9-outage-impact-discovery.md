# RUNBOOK 9 (INTERNAL Codex) — discovery for the "channel/vendor outage → affected use-cases + repos" capability

> **Who runs this: INTERNAL Codex on the box** (has the real `index/` + `mirror/`). **Read-only,
> no writes, don't push** — just report facts (photo). Goal: gather the exact data shapes we need
> to design a fast "a delivery branch is down → which use-cases + repos + components are affected"
> lookup (the messaging team's 25-minute incident-response need). Redact any customer PII / secret
> values — report **structure and counts**, not sensitive cell contents.

Context: the messaging flow is `Ingress → Decision → per-channel Topics → per-channel Delivery
Jobs → Outbound APIs → vendors (CSL/3HK/Sinch/Proofpoint/WhatsApp/WeChat) → Client`. We need to
**invert** it: given a failed vendor/channel/branch, find the topics feeding it → the use-cases
routing to those topics → the repos on the path.

## Q1 — the use-case → topic routing snapshot (MOST IMPORTANT: what IS a "use case")
File: `index/tbl_event_router_usecase_topic.snapshot.csv` (the dev/SCT snapshot).
- Print the **header row** and **5 sample rows** (redact any customer/PII values; keep column names + shape).
- Report: **what columns exist** — is there a use-case id, a topic, a **channel**, a **template**, a
  customer **segment**, an active/enabled flag, a business line (CMB/WPB)?
- Counts: total rows, distinct use-cases, distinct topics, distinct channels.
- **This tells us what a "use case" actually is** (a notification scenario? a template? a channel key?).

## Q2 — the message map (does it capture the delivery chain?)
File: `index/message_edges.csv`.
- Print header + 5 samples. Report the fields (`producer_repo`, `destination`, `consumer_repo`,
  `routing_source`, `evidence`, …).
- Does it capture **topic → delivery-job (consumer)**? Does anything capture **delivery-job →
  outbound-API**? Or is that a normal code dependency (in `internal_edges.csv`) rather than a message edge?
- Coverage: how many repos / topics appear? Is it still the 15-repo pilot, or wider?

## Q3 — how does a delivery job / outbound API know its VENDOR?
Pick one SMS delivery-job repo and one outbound-API repo (e.g. a Sinch / CSL / 3HK one) in `mirror/`.
- Grep their config/code (`application.yml`, `*.properties`, `*.yaml`, code) for vendor/endpoint hints
  (`sinch`, `csl`, `3hk`, `proofpoint`, `smpp`, `smsc`, host/URL keys). Report **whether the
  vendor/carrier is derivable from repo config/code**, or if it lives elsewhere (a DB, the arch diagram).
- Report the repo names you used.

## Q4 — map the diagram's colored nodes to real repo names
Using the repo list + `search_code`, give the real repo name(s) for each colored node:
- Ingress API; Decision Topics; Decision Job.
- Per channel: **Push / SMS / MMS / Email / Letter / WhatsApp / WeChat** — the *Topics* producer repo
  and the *Delivery Job* repo.
- The **Outbound API** repos (CSL / 3HK / Sinch / PFP).
Just the mapping `node → repo name(s)`. (This anchors the topology to our graph.)

## Q5 — end-to-end probe on ONE branch (SMS), with today's data
Try to answer, using our tools + the snapshot: **"if the SMS-via-Sinch branch is down, which topics
feed it, which use-cases route to those topics, and which repos are on the path?"**
Useful commands (read-only):
```
python impact_report.py <sms-delivery-job-repo>
python cli.py repo-routes <sms-delivery-job-repo>
python cli.py consumers <an-sms-topic>     /    python cli.py producers <an-sms-topic>
python cli.py trace --destination <an-sms-topic>
# reverse use-case lookup by hand from the snapshot: which use-case rows have that topic?
```
Report **how far you get** and **exactly where the data runs out** (e.g. "topic→use-case works from
the snapshot, but message map only has pilot repos so the SMS delivery job isn't linked yet").

## Send back (paste this filled in)
```
Q1 snapshot schema:   [ columns; is there channel/template/segment? counts (rows/use-cases/topics/channels) ]
Q2 message map:       [ fields; topic->deliveryjob? deliveryjob->outbound? coverage (pilot or wider) ]
Q3 vendor mapping:    [ derivable from config/code? which keys/repos; or lives elsewhere ]
Q4 node->repo:        [ the mapping for each colored node ]
Q5 SMS probe:         [ how far the branch->topics->use-cases->repos chain resolves today; where it breaks ]
Surprises / errors:   [ ... ]
```

## Why this matters
The answers decide the design: what a "use-case" keys on, whether the vendor/branch layer is
derivable from code or needs a small curated `delivery_topology.json`, and how much is blocked on
the full message map (i.e. on the full-mirror / CodeGraph resource ask). With these facts we can
spec the "outage impact" report + a topic→use-case reverse index precisely.
