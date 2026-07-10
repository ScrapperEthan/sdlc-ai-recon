# RUNBOOK 11 (INTERNAL Codex) — discovery: the MDC repo-list sheet, to tag channel/vendor for ALL repos

> **Who runs this: INTERNAL Codex on the box** (has the real `index/`, `recon_out/`, `mirror/`,
> and the box-local `MDC_Repo_List_Analysis.xlsx`). **Read-only, no writes, don't push** — just report
> facts (photo). Redact any customer PII / secret cell values — report **column names, structure, and
> counts**, not sensitive contents. Goal: gather the exact shape of the boss's per-repo channel sheet so
> we can spec a generator that fills `channel`/`vendor`/`mode` for **all ~390 repos** (today only ~150
> get a channel, from the name alone — 240 are `channel_unknown`).

Context: `make_repo_tags.py` derives `channel` only when the repo **name** contains
`sms/email/push/whatsapp/wechat/letter`, so 240/390 repos have no channel. It already merges an
optional `index/repo_tags.override.json` on top (`--override`). The MDC sheet is the authoritative
per-repo channel/vendor list; you already parsed it once (stdlib, no `openpyxl`) to add ~263 channel
tokens to `index/glossary.json`. This runbook captures the sheet's schema so we can turn those same
rows into a **per-repo override** instead of only glossary tokens.

## Q1 — the sheet's shape (MOST IMPORTANT)
File: `MDC_Repo_List_Analysis.xlsx` (report its exact path on the box).
- List the **sheet/tab names**. For the sheet that holds the repo list: print the **header row** and
  **~5 sample rows** (redact any customer/PII/secret values — keep column names + structure).
- For each column, say what it is: **repo name**, **channel**, **vendor/carrier**, **mode**
  (realtime/batch), **system/business line**, **use-case id**, **active/enabled flag**, **description**,
  anything else. Which column is the **repo-name key**, and does it match our repo names exactly
  (case, `mc-hk-...` prefixes) or need normalising?
- Is it **one row per repo** (single channel/vendor each), or can a repo appear on **multiple rows**
  (multiple channels/vendors)? This decides whether `channel`/`vendor` are scalars or lists.

## Q2 — coverage of the sheet vs our 390-repo universe
- How many **data rows** / **distinct repo names** does the sheet have?
- Of our **390 repos** (keys of `index/repo_tags.json`), how many are **present** in the sheet vs
  **missing**? Print ~10 example repo names that are in our graph but **not** in the sheet — those are
  the ones we'll have to cover by graph propagation or leave as infra.
- Of the sheet's repos, how many have a non-empty **channel** cell? a **vendor** cell? a **mode** cell?
  (These are the ceilings on how far the override can lift each field.)

## Q3 — value vocabularies (so the generator normalises, not guesses)
- **Distinct channel values** as spelled in the sheet (e.g. `SMS`, `Sms`, `sms`, `Push Notification`),
  and how they map to our fixed set `sms/mms/email/letter/whatsapp/wechat/push`.
- **Distinct vendor values** as spelled (e.g. `Sinch`, `CSL`, `3HK`, `Proofpoint`, `WhatsApp`), and
  whether they match the vendors we discovered structurally in RUNBOOK-9/10 (`sinch/csl/htcl/...`).
- **Distinct mode values** (`realtime`/`rt`/`batch`/`bat`/...).
- Any cell using multiple values in one cell (comma/semicolon/newline-separated)? Show one example.

## Q4 — the parse code you already have
- Paste (or point to) the **stdlib snippet** you used to read this `.xlsx` for the glossary
  supplement (the zip → `xl/sharedStrings.xml` + `xl/worksheets/sheet*.xml` walk). We'll reuse the same
  approach in the generator so it runs on the box with **no `openpyxl`**.
- Note any gotchas: merged cells, blank rows, the shared-strings indirection, number-vs-text cells,
  BOM/encoding.

## Q5 — sanity: does a sheet channel agree with a name-derived one?
- Pick ~3 repos whose **name already reveals** the channel (e.g. a `...-sms-deli-job`) and confirm the
  **sheet's channel column agrees**. Pick ~3 repos that are **`channel_unknown` today** but have a
  channel in the sheet (e.g. a shared/decision/ingress repo) — those are exactly the wins the override
  unlocks. List the repo → (name-derived channel or "unknown") → (sheet channel).

## Send back (paste this filled in)
```
Q1 schema:      [ sheet/tab names; header row; ~5 redacted sample rows; which col = repo/channel/vendor/mode/system; one-row-per-repo or multi? ]
Q2 coverage:    [ sheet rows / distinct repos; of our 390: #present / #missing (+10 missing examples); #rows with channel / vendor / mode ]
Q3 vocab:       [ distinct channel values; distinct vendor values; distinct mode values; multi-value-in-one-cell? example ]
Q4 parse code:  [ the stdlib xlsx-read snippet you used + any gotchas ]
Q5 agreement:   [ 3 name-revealed repos: sheet channel agrees? + 3 currently-unknown repos the sheet would fill ]
Surprises/errors:[ ... ]
```

## Why this matters
These answers let me spec the generator precisely (real column names, value normalisation map,
scalar-vs-list) so EXTERNAL Codex can build `enrich_repo_tags.py` → `index/repo_tags.override.json`
in one pass, and RUNBOOK-12 re-runs `make_repo_tags.py` to drop `channel_unknown` from 240 toward 0
(with any leftover unknowns explicitly classified as infra/shared, not silent gaps). If the sheet has
a **vendor** column, it also upgrades vendor-level outage impact from *heuristic* to *authoritative*
for every repo in the sheet — a direct precision win for the 25-min incident-notification use case.
```
