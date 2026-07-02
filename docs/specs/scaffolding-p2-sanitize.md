# Spec: scaffolding Phase 2.1 — sanitize inherited governance/environment values

**Audience:** external Codex IMPLEMENTS (build & test against fixtures); internal Codex
RUNS it against the real `mirror/` and confirms (CONFIRM ON BOX). Read `BACKLOG.md`
"Project context" + "Guardrails", `docs/specs/scaffolding-phase2.md`, and the P2
verification `docs/specs/scaffolding-feedback-p2.md` first. Small follow-up on the
Phase-2 build (commit `d174806`).

## Why

Phase-2 verification (`scaffolding-feedback-p2.md`) confirmed the scaffold is faithful,
but the copied platform/API-metadata files carry the **reference service's own
governance and environment values** into the generated output. No secrets/passwords/
tokens were found, but these fields must NOT be silently inherited by a new service —
it's both a governance issue and a **correctness** bug (a new `payments` service would
otherwise ship with `ingress`'s account IDs, Sonar branch policy, and internal ops URL).

Fix: when copying reference files, replace these values with a `<REVIEW>` placeholder so
the engineer must fill in the new service's own values.

## Confirmed fields to sanitize (from the verification, with evidence)

- `sonar-project.properties`: `sonar.branch.name` (=`dhq_master`),
  `sonar.newCode.referenceBranch` (=`dhq_reference`).
- `SHP/AppConfigFiles/app.yaml`: `sonarAccountID`, `serviceAccountID`, `nexusIQOrgName`,
  `checkMarxTeamPath`.
- `SHP/DeployConfigSchema.yaml`: the internal ops-repo URL and logical-environment
  config paths.
- `src/main/api/doc-properties.json`: internal URLs and the environment link.

Keep as-is (do NOT blank): `sonar.projectKey` / `sonar.projectName` (already rewritten to
the new service name), the service name itself, `sonar.sources` / `sonar.tests`.

## Design (stdlib only)

Apply during the reference-file copy in `scaffold/reference.py`
(`_transform_reference_text` / `_load_reference_files`), AFTER the name-rewrite:

1. **Key denylist** — a module-level `SANITIZE_KEYS` set of the field names above.
   For each copied file, replace the *value* of any denylisted key with `<REVIEW>`:
   - `.properties`: `^(\s*key\s*=\s*).*$` → `\1<REVIEW>`.
   - `.yaml`: `^(\s*key\s*:\s*).*$` → `\1<REVIEW>` (match the leaf key name).
   - `.json` (`doc-properties.json`): `json.load`, walk the object, blank the value of
     any denylisted key (recursively). Re-dump with the same indentation.
2. **Generic URL rule** — in the same files, replace any value that is an absolute URL
   (`https?://…`) with `<REVIEW>`. This catches the ops-repo URL + environment links
   generically, even if a key name differs.
3. Record every sanitized `(file, key)` in `REVIEW_DIFF.md` under a new
   **"Sanitized (fill in before use)"** section, so the reviewer knows exactly what to
   supply.

Make `SANITIZE_KEYS` and the URL rule easy to extend (one obvious place to add keys).
Sanitization runs only on the copied reference files (platform + `src/main/api`
metadata), not on the hand-authored Java/pom/README.

## Note on `src/main/api` reality (align the fixture)

The real `src/main/api` is **RAML + `api.meta` + `doc-properties.json`**, not an OpenAPI
YAML stub (verification item A). Update the fixture
`scaffold/tests/fixtures/mirror/mc-hk-hase-ingress-api/src/main/api/` to that shape
(`<ref>.raml`, `api.meta`, `doc-properties.json`) with **synthetic** values — including a
fake URL and a fake `sonarAccountID`/etc. in the SHP/sonar fixtures — so the sanitizer
has something to act on. Keep all fixture content synthetic (no real coordinates/URLs).

## Fixtures + tests

- Extend the fixtures so the SHP/sonar/api-metadata files contain the denylisted keys
  with fake values and at least one fake `https://…` URL.
- `scaffold/tests` assertions:
  1. Generated `sonar-project.properties` has `sonar.branch.name=<REVIEW>` and
     `sonar.newCode.referenceBranch=<REVIEW>`, but `sonar.projectKey` still = the new
     service name.
  2. Generated `SHP/AppConfigFiles/app.yaml` has `<REVIEW>` for `sonarAccountID`,
     `serviceAccountID`, `nexusIQOrgName`, `checkMarxTeamPath`.
  3. Any `https?://` value in the generated SHP/api-metadata files is `<REVIEW>`.
  4. `REVIEW_DIFF.md` lists each sanitized field.
  5. Existing Phase-2 tests still pass (package derivation, tree shape, starter-only).

## Done when

1. `python -m scaffold.generate payments --out-dir scratch --force` against the mirror
   produces platform/api-metadata files whose governance/environment values and URLs are
   `<REVIEW>`, with the service-name fields still correctly rewritten.
2. `python -m unittest discover -s scaffold/tests` passes.
3. `REVIEW_DIFF.md` has a "Sanitized (fill in before use)" section listing every blanked
   field.

## Verification steps (internal Codex, real mirror) — CONFIRM ON BOX

```bash
python -m unittest discover -s scaffold/tests
python -m scaffold.generate payments --out-dir scratch --force
grep -RInE 'https?://|dhq_master|dhq_reference' scratch/payments/ || echo "clean"
```
Confirm the generated `scratch/payments/` contains **no** inherited account/org/branch/
endpoint value and **no** absolute URL from the reference service (only `<REVIEW>`), while
`sonar.projectKey`/name and the derived package remain correct. Report anything the
denylist + URL rule missed so it can be added. Do NOT commit/push (report as before/after;
the maintainer pushes). Scratch only; never write into a `hase-mc` repo.
