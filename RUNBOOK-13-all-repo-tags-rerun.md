# RUNBOOK 13 (INTERNAL Codex) — re-run all-repo tags after the "others" fix + clean the override

> **Who runs: INTERNAL Codex on the box.** Pull `master` first (HEAD should be `df2c3bb` or later).
> **Read-only over the estate**; writes only generated `index/*.json` + `index/reports/`. Don't push —
> relay results (photo). Background: RUNBOOK-12 exposed a stale override polluting channels with "others".

## Task A — inspect (and likely clean) `index/repo_tags.override.json`
This box-local file had **186 repos forced to `channel:["others"]`** — junk from an earlier pre-correction
attempt, NOT intentional hand-curation. The code now ignores non-channel tokens, so it can't pollute
anymore, but a clean file is better.
```
python -c "import json;d=json.load(open('index/repo_tags.override.json',encoding='utf-8-sig'));import collections;print('entries',len(d));print('channel=others',sum(1 for v in d.values() if [str(x).lower() for x in (v.get('channel') or [])]==['others']))"
```
- If it's almost entirely bulk `channel:["others"]` (as expected), **back it up and remove those entries**
  (keep only any genuine hand fixes): rename to `repo_tags.override.json.bak`, or write a slimmed file with
  the "others" entries dropped. Report what you kept.

## Task B — regenerate and re-photo
```
python enrich_repo_tags.py --report
python make_repo_tags.py ^
  --pom-only-repo mc-hk-hase-aws-pipeline-config ^
  --pom-only-repo mc-hk-hase-commonbus-sdk ^
  --pom-only-repo shp-pipeline-configuration ^
  --pom-only-repo shp-pipeline-shared-lib ^
  --pom-only-repo shp-pipeline-shared-lib-python
```
**Relay (photo) the coverage table + reconcile summary.** Confirm the fix landed:
- **No `others` anywhere in `serves_channels`** (Task D of RUNBOOK-12 previously showed `others` in the
  set — it must be gone now). Spot-check: `python -c "import json;d=json.load(open('index/repo_tags.json'));print('others in serves:', any('others' in (v.get('serves_channels') or []) for v in d.values()))"` → **False**.
- The `amet-mdc-hsbc-svc-rt-hr-*-sms-deli-job` / `-email-deli-job` repos now carry their **name-derived
  channel again** (`[sms]`/`[email]`), not `[others]`. The reconcile report should now show them as real
  **name-vs-sheet mismatches** (name=sms, sheet=Others), which is the correct signal.

### Expected numbers — corrected (do NOT treat these as regressions)
- `channel_unknown ≈ 240`, `channel_set ≈ 150` — the honest name-derived baseline. ✅ expected.
- `serves_channel_set` will be **~150–175, NOT ~390.** My earlier "approach 390" was wrong: the Maven
  dependency graph only proves **library** blast-radius (a lib used by an sms job serves sms), not the
  **messaging pipeline** (ingress→decision→topic→deli-job flows over Kafka, which is not a code dep). So
  pipeline-upstream repos that own no channel stay `true_dark`. Full pipeline coverage needs the message
  map — that's the known resource gap, not a bug here.
- `business_line_set ≈ 47` (real non-empty `CMB/WPB` cells: CMB≈31, WPB≈8/16). The "~151" in RUNBOOK-11
  was a looser tally of a different notion, not the CMB/WPB column. ✅ expected.

## Task C — tests
```
python -m unittest discover -s tests -p "test_*.py"     # elevate if the sandbox blocks temp writes
```
Expect **17 tests, all pass** (the `test_outage_impact` fixture leak is fixed; the new regression test
`test_others_override_neither_clobbers_channel_nor_pollutes_serves` guards the fix).

## Task D — stray file
A new untracked `start-impact-demo.bat` appeared in the working tree during RUNBOOK-12. Report what it is
(one-line summary). If it's a stray/test byproduct, leave it untracked (do NOT commit it).

## Send back (paste this filled in)
```
Task A override:   [ entries; #channel=others; kept what after cleaning ]
Task B coverage:   [ table + reconcile summary; "others in serves" = False?; do the amet-mdc repos show name channel + a real mismatch? ]
Task C tests:      [ 17 pass? ]
Task D bat file:   [ what is start-impact-demo.bat ]
Surprises/errors:  [ ... ]
```
