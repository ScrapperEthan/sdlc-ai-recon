# Handoff: verify scaffolding Phase 2 against the real mirror

**For:** internal Codex (the box that HAS the real `mirror/`).
**About:** Phase 2 is built and pushed — commit `d174806`, spec
`docs/specs/scaffolding-phase2.md`. This confirms it against the real estate.
Read `BACKLOG.md` "Project context" + "Guardrails" first.

> **You cannot commit or push — do not try.** Write the report file locally AND print
> the full report to stdout. The maintainer collects it and pushes it back. Any code
> change you make stays local: report it as a before/after diff for the maintainer.

## Guardrails (do NOT violate)

- **Read-only on production.** Only read `mirror/`. NEVER modify/clone-write/generate
  into a `hase-mc` repo.
- **Scratch only.** The generator must only write under `scratch/` (git-ignored).
- **Stdlib only.** No new dependencies.

## What Phase 2 does (what you're verifying)

`scaffold/generate.py` now (a) makes `--package` optional — deriving the package from
the reference service's namespace convention — and (b) emits a full repo-shaped tree:
`pom.xml` (parent + starter, versions inherited), `sonar-project.properties`, `SHP/*`
config, `.gitignore`, `src/main/api` contract, `src/main/java` (app + resource +
listener), `src/main/resources`, `src/test/java`, and `REVIEW_DIFF.md` citing the mirror
source of every derived file. The SHP/sonar/api files are **read from the reference repo
in the mirror at generate-time and rewritten** with the new service name — nothing is
hardcoded. It stays **starter-only** (no `*-core`).

## Steps (run from the repo root)

```bash
git pull                       # must include commit d174806
python -m unittest discover -s scaffold/tests
python -m scaffold.generate payments --out-dir scratch --force
```

(The generator defaults to `mirror="mirror"`, i.e. `./mirror`, and there is still no
`--mirror` CLI flag — on this box the default is correct.)

## Confirm and report (PASS/FAIL + evidence for each)

1. **Unit tests** pass (expect 15).
2. **No fallback:** the generate command did NOT print `NOTE: mirror ... not found`.
   (If it did, report the real `mirror/` directory names for the parent/starter/reference.)
3. **Derived package** = `com.hsbc.hase.digital.api.payments`. Paste the first line of
   `scratch/payments/src/main/java/.../PaymentsApplication.java` and the real reference
   package it was derived from (`mirror/mc-hk-hase-ingress-api/.../*.java`).
4. **Repo-shaped tree:** `scratch/payments/` contains `sonar-project.properties`,
   `SHP/AppConfigFiles/app.yaml`, `SHP/AppConfigSchema.yaml`, `SHP/DeployConfigSchema.yaml`,
   `.gitignore`, `src/main/api/…`, `src/test/java/…`, plus the pom/java/resources — and
   the SHP/sonar/api files really do match the reference repo's shape with the service
   name swapped. Paste the `find scratch/payments -type f` listing.
5. **Starter-only:** the generated `pom.xml` declares the parent + the starter dependency
   and **no** `*-core` dependency, and restates no Java/Boot version.
6. **Citations resolve:** every `repo/path` in `scratch/payments/REVIEW_DIFF.md` points at
   a file that exists under `mirror/`.

## The two CONFIRM-ON-BOX items (the whole point of this pass)

**A. What is really in `src/main/api`?** Open the reference repo's
`mirror/mc-hk-hase-ingress-api/src/main/api/` and report what it actually holds (OpenAPI
contract? something else?). Confirm the generated `scratch/payments/src/main/api/…` is a
sensible rewrite of it (right filename, service name swapped). If the real content is a
different kind of file than an OpenAPI stub, describe it so the spec can be adjusted.

**B. Do the SHP / sonar files carry secrets or environment-specific values?** Inspect the
real `mirror/mc-hk-hase-ingress-api/{sonar-project.properties, SHP/**}` and the generated
copies under `scratch/payments/`. Report whether any field is a **secret, credential,
endpoint, or per-environment value** that should NOT be copied — even into `scratch/`. If
so, list each such field; the generator will be changed to blank it to a `<REVIEW>`
placeholder. (`scratch/` is git-ignored, but be conservative — this is the governance check.)

## Deliverable

Write your report to `docs/specs/scaffolding-feedback-p2.md` **and** print the same
content to stdout, with sections **"Verification checks"** (items 1–6) and **"CONFIRM-ON-BOX"**
(A and B), each backed by real `repo/path` citations. **Do not commit or push** — the
maintainer collects the file/output and brings it back. That report decides whether Phase 2
is done or needs a small follow-up (e.g. placeholdering a sensitive SHP field).
