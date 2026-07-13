# Spec (EXTERNAL Codex) — reproducible per-bundle CodeGraph build + retrieval bundle-routing

> **Who builds: EXTERNAL Codex.** Verify on the box via `RUNBOOK-17`. Turns the **proven RUNBOOK-16
> recipe** into a re-runnable builder, and wires the per-bundle indexes into retrieval so the Q&A app
> answers deep "who calls / trace" questions. **stdlib-only, read-only over `mirror/` (copies out, never
> writes in), additive. Do NOT touch the Q&A app's agent logic.**

## Confirmed facts from RUNBOOK-16 (build against these — do NOT re-guess the CodeGraph interface)
- `codegraph init [path]` builds an index; `codegraph index [path]` rebuilds. Index lives at
  `<path>/.codegraph/` (`codegraph.db` + WAL). The CLI takes **one project root** — **no** `--out`,
  repo-list, or multi-root, and it **does not traverse Windows junctions**.
- So a bundle must be **materialized as a real directory tree**: copy its repos under one staging root,
  `git init` that root (the repo-root `.gitignore` otherwise hides a staging `repos/`), then
  `codegraph init .` there. Pilot proof: `cd mirror; codegraph init .` → `mirror/.codegraph` (62k nodes).
- CodeGraph's SQLite writes need the box's **elevated local mode**. `mirror/` currently holds only ~16
  repos, so most bundles are partial — **build what's present, report the rest** (full coverage waits on
  the full mirror clone).
- Proven per-bundle build (RUNBOOK-16 Task A): `misc-ingress-to-lys` 11 repos → 378 s / 125 MiB;
  `tracking` 13 → 465 s / 137 MiB. Real resolution works: `codegraph explore IngressService` → 84
  symbols, 13 cross-repo callers (not lexical fallback).

## Building block 1 — `build_codegraph.py` → `index/codegraph/<bundle>/` + `index/codegraph_build.json`
Reads `index/bundles.json` (`{bundle: {primary:[…], with_libs:[…]}}`). For each bundle, in the plan
order from `docs/DOMAIN-PARTITION-PLAN-zh.md §6` (ingress, tracking, platform-core, then the rest):
- `repos = primary ∪ with_libs`, filtered to those present under `mirror/<repo>/` (skip + count the rest).
- **Stage**: `shutil.copytree` each present repo into `index/codegraph/<bundle>/<repo>/`, ignoring
  `.git`, `.codegraph`, `target`, `build`, `node_modules` (keep each repo's own `.gitignore`). Then
  `git init -q` the staging root (so CodeGraph sees the copied sources).
- **Build**: `subprocess.run(["codegraph","init","."], cwd=staging_root)`. Record `returncode`, wall
  seconds, and `.codegraph/codegraph.db` size (MiB).
- Write a manifest `index/codegraph_build.json`: `{generated_at, bundles:[{bundle, root, staged_repos,
  staged_count, missing_count, returncode, seconds, db_mib, error?}]}`. A bundle with 0 present repos →
  `{bundle, skipped:"no repos in mirror", missing_count}`.
- Flags: `--only <bundle>`, `--dry-run` (print the present/total plan, build nothing), `--mirror`,
  `--out-root`, `--bundles`, `--manifest`. Missing `codegraph` on PATH → clean error, not a crash.
- Config (block 2). Re-runnable: a stale staging dir for a bundle is removed before re-staging.

## Building block 2 — config paths
Add to `retriever/config.py`: `CODEGRAPH_ROOT = index/codegraph`, `CODEGRAPH_BUILD_JSON =
index/codegraph_build.json` (env-overridable like the rest).

## Building block 3 — retrieval bundle-routing (so queries hit the right index)
Today `retriever/unified_impact.py::_call_graph` shells `codegraph explore <seed>` in the **process
cwd** — wrong once indexes are per-bundle. Add routing:
- `_built_roots()` — read the manifest, return `{bundle: root}` for entries with `returncode == 0`.
- `bundle_root_for(seed, bundle=None)` — resolve the staging root to run in: (1) explicit `bundle` arg
  if built; else (2) if `seed` is a repo in `repo_tags.json`, use its `bundle` field → root; else (3) a
  built bundle whose staging dir contains a `<seed>` repo dir; else `None`.
- `_call_graph(seed, cwd=None)` — pass `cwd` to `subprocess.run`; include `bundle_root` in the result.
- `query(seed, transitive=False, bundle=None)` — route via `bundle_root_for`, add `bundle_root` to the
  payload. `None` root → current cwd behaviour (back-compatible), so nothing regresses pre-build.
- **Fix the CLI gap RUNBOOK-16 found:** add a `unified-impact` subcommand to `cli.py`
  (`python cli.py unified-impact <seed> [--bundle B] [--transitive]` → prints `unified_impact.query`).

## Tests (stdlib fixtures)
- `build_codegraph.plan()` (a pure function): a fake `bundles.json` + a fake `mirror/` with some repo
  dirs → correct present/missing split and plan order; `--dry-run` writes nothing.
- staging: `stage_bundle` copies sources, excludes `.git`/`target`, and leaves a `.git` in the staging
  root (mock/av​oid the real `codegraph` — test staging only, not the build).
- routing: a fake manifest + `repo_tags.json` → `bundle_root_for` resolves by explicit bundle, by repo's
  bundle, and returns `None` (falls back) when unbuilt. Monkeypatch `shutil.which`/`subprocess` so no
  real `codegraph` is needed.

## Honesty / limits (put in the report + PLAN)
- **All 31 bundles need the full mirror** (~390 repos). Only the ~3 present bundles build today; the rest
  skip honestly until the mirror is cloned on the approved machine.
- CodeGraph needs **elevated mode** for SQLite writes; staging **copies** repos (disk-heavy, no junctions).
- Symbol→bundle routing is best-effort (repo/bundle hint is most reliable); cross-bundle symbols may need
  the caller to pass `--bundle`. Cross-bundle **dependency + message** impact already works (full graphs).
