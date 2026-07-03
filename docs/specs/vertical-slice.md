# Spec: the vertical slice — generate a real change, compile + test it, emit a diff

**Audience:** external Codex IMPLEMENTS (build & test against fixtures — no `mirror/`,
no Maven on its box); internal Codex RUNS it against the real `mirror/` + real Maven.
Read `BACKLOG.md` "Project context" + "Guardrails", `PROJECT-STATE.md`, and the
scaffolding specs first. This is the **next capability after Step 2 scaffolding**: turn
"generate a skeleton" into "generate a real code change to an existing service, prove it
compiles and its tests pass, and hand a diff for review."

## Current status (2026-07-03)

**Step 0 PASSED on the internal box.** Toolchain landed (Zulu JDK 21 + Apache Maven 3.9.6);
an *unmodified* `mc-hk-hase-ingress-api` copied to `scratch/probe/` compiled and tested
green (`COMPILE_EXIT=0`, `TEST_EXIT=0`). So building a HASE service outside its repo is
feasible — the feasibility gate is cleared.

**First real slice run (2026-07-03) surfaced one Windows-portability bug, now fixed.**
`python -m change.add_endpoint mc-hk-hase-ingress-api --path /status --out-dir scratch`
generated a correct change — the `@GetMapping("/status")` was inserted into the existing
`IngressResource.java`, `mirror/` was untouched (3659 files hash-identical), and running
`mvn.cmd -q test` in the scratch copy **passed** — but the tool itself crashed with
`[WinError 2]` *before* emitting `CHANGE_DIFF.md` / `BUILD_RESULT.md`. Root cause: on
Windows Maven is `mvn.cmd`, and `subprocess.run(("mvn",...), shell=False)` can't find a
bare `mvn`. Fixed in `change/build.py`: `_resolve_command` resolves `mvn`→`mvn.cmd` via
`shutil.which`, and a failed launch is now recorded as a build failure (so the review
artifacts are always emitted) instead of crashing the run. **Re-run the full command on
the box to confirm the tool now emits `CHANGE_DIFF.md` + `BUILD_RESULT.md` (PASS) itself.**

The build runner stays mock-injectable (`runner=` / `--skip-build`) so the edit + diff
logic remains testable without a toolchain.

## Why

Scaffolding proves we can create a convention-faithful *new* service. The higher-value,
more credible capability is a thin **end-to-end slice** of the everyday loop:
understand → change → **verify (compile + test)** → diff for review. The "verify" step is
what makes an AI change *trustworthy* — it checks its own output before a human sees it,
and it pulls the Test + Build stages of the SDLC into the product.

## Guardrails (do NOT violate)

- **Read-only on production / the mirror.** Never modify a repo under `mirror/`. Copy the
  target service into `scratch/` and change the **copy** only. Never write into `hase-mc`.
- **Scratch only.** All output (the modified copy, the diff, the build log) lives under
  `scratch/`.
- **Stdlib for our tooling.** Orchestration is Python stdlib; it *invokes* Maven (`mvn`)
  as an external process — that's the one allowed external tool, and only on the internal
  box that already has it.

## Step 0 — feasibility gate (CONFIRM ON BOX, do this first)

The whole slice depends on being able to build a HASE service outside its repo. Before
building the change logic, internal Codex confirms, on the real box:

1. `mvn -v` works; note the version.
2. Copy one pilot service (e.g. `mc-hk-hase-ingress-api`) from `mirror/` to
   `scratch/probe/` and run `mvn -q -DskipTests compile` **unmodified**.
3. Then `mvn -q test`.

Report: do compile and test succeed on an **unmodified** copy in scratch? Are the parent
POM / starter / dependencies resolvable from the internal Maven repo (online), or is an
offline (`-o`) build needed? **If an unmodified service can't build in scratch, stop and
report why** — that's the real blocker to solve before the change logic is worth building.

## Phase 1 scope (smallest provable slice): add a GET endpoint

Keep the *change* trivial and templated so the value is in the **verify loop**, not in NL
understanding. Task = "add a health-style GET endpoint to an existing service."

CLI (new module `change/`, mirrors `scaffold/`):
```
python -m change.add_endpoint <service-repo> --path /status --method status [--mirror mirror] [--out-dir scratch]
```

Steps the tool performs:
1. **Copy** `mirror/<service-repo>` → `scratch/<service-repo>-change/` (read-only source).
2. **Locate** the existing `@RestController` class in the copied source (reuse the same
   package/style detection as `scaffold/reference.py`). Pick the one under the `resource`
   (or `controller`) package.
3. **Apply the change** in the house style: add a new `@GetMapping("<path>")` method
   returning a simple body, matching the surrounding controller's imports/annotations/
   indentation. If a suitable controller isn't found, add a new `*Resource` class next to
   the existing one rather than guessing.
4. **Generate/extend a test** that asserts the new method exists / returns OK (a minimal
   `@WebMvcTest` or a plain unit test consistent with the repo's existing test style).
5. **Build + test**: run `mvn -q test` in the scratch copy; capture exit code + the tail
   of the output.
6. **Emit for review** under `scratch/<service-repo>-change/`:
   - `CHANGE_DIFF.md` — a unified diff (stdlib `difflib`) of every file changed vs the
     mirror original, plus the exact files touched.
   - `BUILD_RESULT.md` — the `mvn` command, pass/fail, and the output tail.

## Where

- New `change/` package: `change/add_endpoint.py`, `change/__init__.py`,
  `change/build.py` (wraps `mvn`, captures output), `change/tests/` with fixtures.
- Reuse `scaffold/reference.py` helpers for locating packages/classes (import, don't copy).
- `.gitignore` already covers `scratch/`.

## Fixtures + tests (external Codex, no mirror / no Maven)

- Fixture: a tiny Spring-style service tree under `change/tests/fixtures/` with one
  `@RestController`.
- `change/build.py` must be **injectable/mocked**: tests pass a fake runner (or set an env
  flag) so the suite verifies the *edit + diff* logic without invoking real `mvn`.
- Tests assert: (1) the new `@GetMapping("/status")` method is inserted into the existing
  controller; (2) a test file is added/updated; (3) `CHANGE_DIFF.md` shows only the
  intended files; (4) the copy is under `--out-dir`, the fixture source is untouched;
  (5) `..`/path-escape rejected; (6) with a mocked failing build, `BUILD_RESULT.md`
  records the failure and the tool exits non-zero.

## Done when

1. Against the fixture, `python -m change.add_endpoint <svc> --path /status` produces a
   scratch copy with the endpoint added, a test added, and `CHANGE_DIFF.md` +
   `BUILD_RESULT.md`, with the mocked build reported. Unit tests pass.
2. The mirror source / fixture source is never modified.

## Verification steps (internal Codex, real mirror + real Maven)

After Step 0 passes:
```
python -m change.add_endpoint mc-hk-hase-ingress-api --path /status --out-dir scratch
```
Confirm: the new endpoint is added in the house style; `mvn test` in the scratch copy
runs and **passes** (report the result either way); `CHANGE_DIFF.md` shows a clean, small,
reviewable diff; nothing under `mirror/` changed. Report the build log tail.

## Explicitly deferred (NOT Phase 1)

- Natural-language intent → change (Phase 1 takes an explicit `--path/--method`, not free
  text). Wiring NL intent + the retrieval layer to *decide* the change comes later.
- Message listeners, DB/DAO changes, cross-repo changes, `*-core` edits.
- Auto-committing or PR-raising the change — output stays a scratch diff for a human.

## Needs a decision (maintainer)

- **Step 0 result** governs everything: if services can't build in scratch on the box
  (deps/offline), we solve that first (build environment) before the change logic.
- Endpoint style: confirm the house REST convention (base path, response type, whether a
  new method vs a new `*Resource` class is preferred) from the reference service.
