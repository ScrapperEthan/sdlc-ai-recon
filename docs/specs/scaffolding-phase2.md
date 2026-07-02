# Spec: scaffolding Phase 2 — a repo-shaped, convention-faithful thin service

**Audience:** external Codex IMPLEMENTS (no `mirror/` — build & test against the
fixtures under `scaffold/tests/fixtures/`); internal Codex RUNS it against the real
`mirror/` and confirms the two items flagged **CONFIRM ON BOX**. Read `BACKLOG.md`
"Project context" + "Guardrails" and `docs/specs/scaffolding.md` (Phase 1) first.
Phase 1 verification passed — see `docs/specs/scaffolding-feedback.md`.

## What the mirror evidence changed (why P2 is NOT an api/core split)

The Phase-1 investigation (`scaffolding-feedback.md`, Task B) found, from the real
mirror:

- The only real api/core pair — `mc-hk-hase-ingress-api` (shell) + `mc-hk-hase-api-ingress-core`
  (core) — are **two independent top-level git repos, not Maven modules** in one repo.
- The shell's POM depends **only on the starter** (`mirror/mc-hk-hase-ingress-api/pom.xml:22`),
  **not** on its own `*-core`. The shared cores come in transitively via the starter/parent.
- The other visible business repos are **jobs** (`mc-hk-hase-svc-*-job`), a different
  archetype, also starter-based, with no paired core.

**Decision:** the default new-service scaffold is a **single thin `*-api` repo on the
starter**, made structurally faithful to a real repo. **Do not** generate a `*-core`
and **do not** auto-wire a core dependency. Core generation is deferred (see end).

## Goal

`python -m scaffold.generate <name>` produces `scratch/<name>/` that is structurally
**indistinguishable from a real HASE thin-service repo**: the correct package
convention, starter-only dependency (already from Phase 1), the platform/config files a
real repo carries, and the same on-disk layout — so a HASE engineer (and a reviewer)
recognizes it as "one of ours."

## Scope — Phase 2 items

### 1. Auto-derive the package (make `--package` optional)

The real convention is `com.hsbc.hase.digital.api.<service>` (evidence:
`mirror/mc-hk-hase-ingress-api/src/main/java/com/hsbc/hase/digital/api/ingress/Application.java:4`).
Requiring the user to type `--package` invites getting it wrong (our Phase-1 demo used
the non-conforming `com.hsbc.hase.payments`).

- If `--package` is omitted: derive the **base namespace** from the reference service by
  taking its detected package and stripping the trailing segment equal to the
  reference's own short name (`…api.ingress` → base `…api`), then append the new
  service slug → `com.hsbc.hase.digital.api.<slug>`.
- If `--package` is given, it overrides (keep the existing validation).
- **If the mirror is absent** (external box), `--package` stays **required** — do NOT
  hardcode the real base namespace into this public repo (see Security note).
- `reference.py` should expose the base namespace + a citation; `generate.py` uses it
  only when `--package` is not supplied.

### 2. Add the platform/config files a real shell repo carries

A real shell repo (`mirror/mc-hk-hase-ingress-api`) contains, beyond what we emit today:
`sonar-project.properties`, `SHP/AppConfigFiles/app.yaml`, `SHP/AppConfigSchema.yaml`,
`SHP/DeployConfigSchema.yaml`, `.gitignore` (full listing in `scaffolding-feedback.md`).

- When the mirror is present, **derive these from the reference repo at generate-time**:
  read each file, substitute the reference service name/slug/package with the new one,
  write the result under `scratch/<name>/`. Do NOT hardcode their contents into this
  public repo — same "derive from the mirror" discipline as the POM coordinates.
- When the mirror is absent, emit a minimal `.gitignore` + a `README` note that the
  SHP/sonar files are produced only against the mirror.
- **CONFIRM ON BOX:** before shipping, internal Codex confirms the SHP/sonar files carry
  **no secrets or environment-specific values** that shouldn't land even in `scratch/`.
  If any field is per-environment, replace it with an obvious `<REVIEW>` placeholder and
  list it in `REVIEW_DIFF.md`. (`scratch/` is git-ignored, but be conservative.)

### 3. Match the source layout

Real shell layout: `src/main/api`, `src/main/java`, `src/main/resources`,
`src/test/java`. We currently emit `src/main/java` + `src/main/resources` only.

- Add `src/main/api/` and `src/test/java/<package path>/` (with a placeholder test).
- **CONFIRM ON BOX:** what actually lives in `src/main/api` in the reference repo (looks
  like API/contract specs — OpenAPI?). Record the finding; if it's a contract file,
  generate a stub of the same kind and cite the reference.

### 4. Keep starter-only (no core)

Unchanged from Phase 1: the POM declares the parent + starter dependency and nothing
else. Do NOT add a `*-core` dependency.

## Security note (this is a PUBLIC GitHub repo)

Do **not** commit the real internal coordinates/versions (`com.hsbc.hase.digital`,
`4.13.x`) or the full base namespace into this repo. The generator derives them from the
mirror at runtime on the internal box (verified: `from_mirror=True`, no fallback). The
`DEFAULTS` in `scaffold/reference.py` stay generic placeholders; fixtures keep their
synthetic coordinates.

## Where

- `scaffold/reference.py` — add base-namespace derivation + citation; add a helper to
  read/transform a reference repo's platform files (stdlib only).
- `scaffold/generate.py` — make `--package` optional (derive when absent), emit the new
  files/dirs, extend `REVIEW_DIFF.md` to cite every new file's source.
- `scaffold/tests/` — extend fixtures (add `sonar-project.properties`, an `SHP/` tree,
  `src/main/api` to the fixture `mc-hk-hase-ingress-api`) and add tests (below).

## Done when

1. `python -m scaffold.generate payments` (NO `--package`) against the mirror produces
   `scratch/payments/` whose Java package is `com.hsbc.hase.digital.api.payments`,
   derived + cited from the reference service.
2. The scratch tree contains `sonar-project.properties`, `SHP/AppConfigFiles/app.yaml`,
   `SHP/AppConfigSchema.yaml`, `SHP/DeployConfigSchema.yaml`, `.gitignore`,
   `src/main/api/`, `src/test/java/…` — matching the real shell layout, each cited in
   `REVIEW_DIFF.md`.
3. `python -m unittest discover -s scaffold/tests` passes, including: package derived
   from the fixture reference; `--package` still overrides; mirror-absent path still
   requires `--package`; generated files stay inside `--out-dir`; scratch-only unchanged.
4. Running with the mirror absent degrades gracefully (minimal files + a NOTE), no crash.

## Verification steps (internal Codex, real mirror)

```bash
python -m unittest discover -s scaffold/tests
python -m scaffold.generate payments --out-dir scratch --force
```
Confirm: derived package = `com.hsbc.hase.digital.api.payments`; the SHP/sonar files
match the reference repo's shape with the name swapped and **no secrets**; `src/main/api`
handling matches what the reference repo actually holds. Report findings for the two
**CONFIRM ON BOX** items. Do NOT commit/push (report as before/after + findings; the
maintainer pushes). NEVER write into a `hase-mc` repo — scratch only.

## Explicitly deferred (NOT in Phase 2)

- **`*-core` generation / `--with-core`.** Evidence says new shells are starter-only. If
  a real case appears, emit core as a **separate repo-shaped scratch folder** (never a
  submodule) with **no auto-wired dependency**, behind a maintainer decision and a fuller
  mirror sample.
- **A `--type job` archetype.** The mirror shows `*-job` repos as a distinct, common
  pattern (batch/streaming, starter-based). Worth a future variant; note, don't build now.
