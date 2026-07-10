# RUNBOOK 12 (INTERNAL Codex) — verify all-repo tags (MDC business metadata + graph serves_channels)

> **Who runs: INTERNAL Codex on the box** (real `MDC_Repo_List_Analysis.xlsx`, `recon_out/`, `index/`).
> Runs the code EXTERNAL Codex built from `docs/specs/all-repo-tags.md` (**pull `master` first**).
> **Read-only over the estate**; writes only generated `index/*.json` + `index/reports/`. Don't push —
> relay results (photo). Remark is free text → don't paste sensitive cell contents; report structure/counts.

## Task A — ingest the MDC sheet → additive metadata
```
python enrich_repo_tags.py
```
Writes `index/repo_tags.mdc.json` + prints a coverage table. **Relay (photo) the table.** Confirm it
parsed with **no `openpyxl`** (stdlib xlsx), read the `full Repository List` tab, and handled the
header typos (`WhatsAPP`/`Maraketing`/`TimeCritcal`). Sanity: per-field non-empty counts should land
near the RUNBOOK-11 tallies — marketing/servicing ≈103, time-critical ≈13, business ≈151,
fixed-channel-declared ≈129. `Others` must be bucketed as `other`, **not** a real channel.

## Task B — regenerate tags with serves_channels + MDC fields
```
python make_repo_tags.py ^
  --pom-only-repo mc-hk-hase-aws-pipeline-config ^
  --pom-only-repo mc-hk-hase-commonbus-sdk ^
  --pom-only-repo shp-pipeline-configuration ^
  --pom-only-repo shp-pipeline-shared-lib ^
  --pom-only-repo shp-pipeline-shared-lib-python
```
**Relay (photo) the new coverage table.** What we're checking (the honest headline):
- `channel_unknown` **stays ~240** (expected — the sheet doesn't fill it; don't be alarmed).
- `serves_channel_set` should approach **390** (every repo now has a channel blast-radius), and the
  **true-dark set** (`channel_unknown` AND `serves_channels==[]` AND not `mdc_common`) should be small.
- New rows present: `serves_channel_set`, `marketing_servicing_set`, `time_critical_set`,
  `mdc_common_set`, `channel_explained`.

## Task C — reconciliation report
```
python enrich_repo_tags.py --report      # or the separate reporter, per the spec
type index\reports\TAG_RECONCILE.md
```
Confirm it lists **mismatches** (name has a channel, sheet says `Others`/blank — the
`amet-mdc-hsbc-svc-rt-hr-csl-sms-deli-job` / `…-cm-sms-deli-job` / `…-int-email-deli-job` cases from
RUNBOOK-11 should appear), a **confirmations** count, and **explained unknowns** (how many of the ~240
are `mdc_common`/`Others`/covered by `serves_channels`). Structural name channel must still **win** in
the tags (the report flags, it does not overwrite).

## Task D — spot-check serves_channels on a real shared repo
Pick one shared/infra repo that is `channel_unknown` by name (e.g. a `-commonbus-sdk`, a
`-decision-*`, or an ingress repo). Show its `serves_channels` from `index/repo_tags.json`, then:
```
python cli.py impact <that-repo> --transitive
```
Confirm the channels in `serves_channels` are exactly the channels **owned by the delivery jobs in
that impact set** (blast-radius is correct, not invented).

## Send back (paste this filled in)
```
Task A mdc ingest:   [ coverage table; parsed stdlib/no openpyxl? counts near 103/13/151/129? Others=other not channel? ]
Task B new coverage: [ full table; channel_unknown still ~240? serves_channel_set (→390?); true-dark count; new rows present? ]
Task C reconcile:    [ mismatches listed (the amet-mdc sms/email->Others ones)? confirmations count; explained-unknown count ]
Task D serves check: [ the repo; its serves_channels; does it match cli.py impact's delivery-job channels? ]
Surprises / errors:  [ ... ]
```

## What this establishes
Green = every repo now carries **business metadata** (marketing/servicing, time-critical, business
line, common-flag — new axes for narrow-first retrieval and incident triage) **and** an honest
all-repo **channel blast-radius** (`serves_channels`), while structural channel/mode/system stay
name-authoritative. It also proves — with numbers — the honest story: the sheet **confirms** channels
and adds business context, but the 240 name-unknowns are covered by **graph blast-radius**, not by the
sheet. Next: wire `serves_channels` into `outage_report.py` so `channel:sms` returns the full affected
set (owners **+** servers), and decide whether a vendor sheet is worth requesting.
