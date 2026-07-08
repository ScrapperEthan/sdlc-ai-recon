# RUNBOOK 7 — generate the real bundle plan + verify the retrieval service (on the box)

The retrieval service, the bundle planner, and the `cross-repo-impact` skill are now on
`master` (merged from `codex/retrieval-service-bundles`). This RUNBOOK is the **internal Codex**
follow-up: (A) generate the **real** `index/bundles.json` from the full dep graph and relay the
review table; (B) prove the retrieval service runs **beside the unchanged Q&A app** and its
endpoints match `cli.py`; (C) optional — run the `cross-repo-impact` skill end to end.

**Prereqs:** `git pull` on `master`. You have the real `recon_out/internal_edges.csv` (full dep
graph), `mirror/` (pilot), and `index/`.

**Safety:** read-only except Task A writes exactly one file, `index/bundles.json` (a generated
index artifact — **not** `mirror/`). Don't push (relay by photo). Don't modify `mirror/`.

> Windows note: in PowerShell use **`curl.exe`** (bare `curl` is an alias for
> `Invoke-WebRequest`). Run the two servers in two separate terminals.

---

## Task A — generate the real partition plan + review table

Include the 5 pom-only infra repos that the dep graph doesn't cover (from RUNBOOK 6):

```
python make_bundles.py ^
  --pom-only-repo mc-hk-hase-aws-pipeline-config ^
  --pom-only-repo mc-hk-hase-commonbus-sdk ^
  --pom-only-repo shp-pipeline-configuration ^
  --pom-only-repo shp-pipeline-shared-lib ^
  --pom-only-repo shp-pipeline-shared-lib-python
```

This writes `index/bundles.json` and prints a review table. **Relay (photo) the whole printed
table** — every bundle's `primary / total / est_mib / flags`, plus the `Primary coverage: N/M`
line at the top. We care about:
- Coverage = 100% (N == M, no unassigned repos — the script asserts this).
- Which bundles are **flagged** (`repos>60` or `mib>600`) — those are the ones we split further.
- How the `svc-*` / `ssvc-*` sub-splits and the `misc-*` merges actually landed.

We review the table together and tune `--merge-min` / any manual overrides before locking it.
(You can re-run with a different `--merge-min` to compare; it's cheap and read-only.)

## Task B — service runs beside the Q&A app, endpoints match `cli.py`

1. **Terminal 1** — retrieval service:  `python retrieval_service.py`  (defaults `127.0.0.1:8848`).
2. **Terminal 2** — the existing Q&A app:  `python -m webapp.server`.
   **Confirm BOTH start and stay up at the same time** — this is the "Q&A app still works,
   untouched" check.
3. **Health:**  `curl.exe -s http://127.0.0.1:8848/health`  → `{"ok": true, "indexed_as_of": ...}`.
4. **Parity** on a real mirror repo (pick one that's actually in `mirror/`), compare the two:
   ```
   curl.exe -s "http://127.0.0.1:8848/impact?repo=<repo>&transitive=1"
   python cli.py impact <repo> --transitive
   ```
   → same JSON. Repeat for one of `/consumers?destination=<dest>` and `/trace?use_case_id=<id>`.
5. **Bad input:**  `curl.exe -s -o NUL -w "%{http_code}" "http://127.0.0.1:8848/impact?repo=nope"`
   → **404** (unknown repo). A POST → **405**.

## Task C — (optional) run the `cross-repo-impact` skill end to end

Only if an agent runtime that honors `execute` is handy on the box (opencode/Codex):
1. `set RETRIEVAL_BASE_URL=http://127.0.0.1:8848`
2. Point the runtime at `docs/skills/cross-repo-impact/SKILL.md`, run it on one real target
   (a repo, `use-case:<id>`, or `topic:<name>`).
3. Confirm it produces a `CROSS_REPO_IMPACT_<flow>.md` with `repo/path:line` citations and
   **touches nothing in `mirror/`**.

## Send back (paste this filled in)

```
Task A  coverage:        [ N/M — is it 100%? ]
        flagged bundles: [ list bundles with repos>60 or mib>600, + their counts ]
        table:           [ photo the full printed table ]
Task B  both up:         [ service + Q&A app running together? Y/N ]
        /health:         [ indexed_as_of value ]
        parity:          [ do service JSON and cli.py match for impact/consumers/trace? Y/N ]
        404/405:         [ bad repo → 404? POST → 405? ]
Task C  (optional):      [ skill produced cited CROSS_REPO_IMPACT_*.md? mirror untouched? ]
Surprises / errors:      [ ... ]
```

**Then:** with the real table we lock the domain list + build order; Task B green means the moat
is now curl-reachable (the join point for Copilot agents), running estate-wide on the already-full
dep graph — mirror/CodeGraph scale-up proceeds per bundle from there.
