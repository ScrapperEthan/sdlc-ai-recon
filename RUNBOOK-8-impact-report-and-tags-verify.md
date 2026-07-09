# RUNBOOK 8 (INTERNAL Codex) — verify impact report + generate real repo tags/glossary

> **Who runs this: INTERNAL Codex on the box** (has the real `recon_out/`, `index/`, `mirror/`).
> It runs & verifies the code EXTERNAL Codex built from `docs/specs/impact-report-and-repo-tags.md`
> (pull `master` after that branch is merged). **Read-only over the estate**; writes only generated
> `index/*.json`. Don't push — relay the results (photo). Don't modify `mirror/`.

## Task A — verify the impact report on real targets

Pick one real repo, one real topic, and (if you have a use-case id) one use-case.

```
python impact_report.py <a-real-repo>                 # e.g. an ingress/tracking service
python impact_report.py topic:<a-real-topic>
python impact_report.py use-case:<id>                 # if available
```
Confirm the report shows: **Upstream** (what it depends on), **Downstream** (who depends on it —
direct + transitive), **Async routes** (producers/consumers per topic), **Channel**, **Risk
callouts** (hubs; the honesty note about DB-table routing), and **citations** on each claim.
Spot-check one downstream claim against `python cli.py impact <repo> --transitive` — they must agree.

Also hit the endpoint (start `python retrieval_service.py` first):
`curl.exe -s "http://127.0.0.1:8848/impact-report?target=<repo>"` → same content as JSON.

## Task B — generate the REAL repo tags + place the glossary

### B1 — glossary (place the real one, box-local)
Save the seed glossary Claude provided (in chat) to `index/glossary.json` (extend it from the
team's naming sheet as you like — it's gitignored, stays on the box). Sanity:
`python -c "from retriever import glossary; print(glossary.expand('svc-rt-hr'))"` → tokens annotated.

### B2 — real per-repo tags
```
python make_repo_tags.py ^
  --pom-only-repo mc-hk-hase-aws-pipeline-config ^
  --pom-only-repo mc-hk-hase-commonbus-sdk ^
  --pom-only-repo shp-pipeline-configuration ^
  --pom-only-repo shp-pipeline-shared-lib ^
  --pom-only-repo shp-pipeline-shared-lib-python
```
This writes `index/repo_tags.json` and prints a **coverage table**. **Relay (photo) the table** — we
want to see: how many repos got a `system` / `channel` / `mode`, and how many are `other`/unknown
(= what still needs manual curation in `index/repo_tags.override.json`).

### B3 — filter endpoint
With the service running:
`curl.exe -s "http://127.0.0.1:8848/repos?channel=sms&mode=realtime"` → a sane repo list.
Try `channel=whatsapp`, `channel=push`, `system=amet-mdc` to spot-check.

## Send back (paste this filled in)
```
Task A  impact report:   [ upstream/downstream/async/channel/citations all present? Y/N ]
        cli agreement:   [ downstream set matches `cli.py impact`? Y/N ]
        /impact-report:  [ endpoint returns same content? Y/N ]
Task B1 glossary:        [ expand('svc-rt-hr') output ]
Task B2 tags coverage:   [ photo the coverage table: #system/#channel/#mode set, #unknown ]
Task B3 /repos filter:   [ sms+realtime count; whatsapp count; amet-mdc count ]
Surprises / errors:      [ ... ]
```

## What this unlocks
Task A green = the **impact/use-case → channel + up/downstream, cited** capability works on the real
estate (the coding team's pain point + leadership's ask). Task B green = retrieval is now
**narrow-first** (filter by channel/mode/system) and answers understand the abbreviations. The
coverage table tells us how much manual curation is actually left (likely small).
