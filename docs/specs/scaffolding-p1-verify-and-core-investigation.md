# Handoff: verify scaffolding P1 + investigate the api/core structure

**For:** internal Codex (the box that HAS the real `mirror/`).
**From:** the scaffolding spec `docs/specs/scaffolding.md` (Step 2, item #11 in `BACKLOG.md`).
**What we need back:** run Task A, answer Task B, and produce a report (see
"Deliverable" at the bottom).

> **You cannot commit or push — do not try.** Write the report file locally AND print
> the full report to stdout. The maintainer will collect it and push it back. Any code
> change you make (e.g. to `DEFAULTS`) stays local: report it as a before/after diff so
> the maintainer can apply and commit it.

Read `BACKLOG.md` "Project context" + "Guardrails" first.

## Guardrails (do NOT violate)

- **Read-only on production.** Only read `mirror/`. NEVER modify, clone-write, or
  generate into a `hase-mc` repo.
- **Scratch only.** The generator must only write under `scratch/` (git-ignored).
- **Stdlib only.** Don't add dependencies.
- This is investigate-and-report + a small confirm/fix. Do not build Phase 2 yet —
  that waits on the decision your report enables.

## Background (what P1 already did)

Commit `b342dff` evolved `scaffold/generate.py` + added `scaffold/reference.py` so the
generated `pom.xml` **inherits the real parent POM** `mc-hk-hase-api-parent` and depends
on the shared starter `mc-hk-hase-api-starter`, with coordinates **derived from the
mirror** (not hardcoded). On a box without the mirror it falls back to placeholder
defaults in `scaffold/reference.py` (`DEFAULTS`, including `version: "<CONFIRM>"`). Your
box has the real mirror, so it should use real values — this task confirms that.

---

## Task A — Verify P1 against the real mirror

Run from the repo root (the generator defaults to `mirror="mirror"`, i.e. `./mirror`;
there is no `--mirror` CLI flag yet, and on this box it should not need one):

```bash
python -m unittest discover -s scaffold/tests
python -m scaffold.generate payments --package com.hsbc.hase.payments \
  --reference mc-hk-hase-ingress-api --out-dir scratch --force
```

Then **confirm each of these against the real mirror** and report pass/fail + evidence:

1. The command did **not** print `NOTE: mirror ... not found`. (If it did, the parent
   or starter repo directory name under `mirror/` differs from what
   `scaffold/reference.py` expects — report the real directory names.)
2. The generated `scratch/payments/pom.xml` `<parent>` block (groupId / artifactId /
   version) **exactly matches** `mirror/mc-hk-hase-api-parent/pom.xml`. Paste both.
3. The generated starter dependency matches `mirror/mc-hk-hase-api-starter`
   coordinates.
4. The detected `base_package` matches the real package used by the reference service
   `mc-hk-hase-ingress-api`.
5. The pom restates **no** `java.version` and **no** Spring Boot version (they must be
   inherited from the parent).
6. `scratch/payments/REVIEW_DIFF.md` citations point at real files that exist under
   `mirror/`.

**If the real coordinates differ from the `DEFAULTS` in `scaffold/reference.py`**
(especially the `<CONFIRM>` version placeholder and `base_package`): edit `DEFAULTS` to
the real values **locally** and re-run to confirm the fix works, then report the exact
before/after (a diff) so the maintainer can commit it. Do NOT push the change yourself.

Report in `scaffolding-feedback.md`: the generated `pom.xml`, the real parent + starter
coordinates you found, whether `DEFAULTS` needed changing (before/after), and the unit
test result.

---

## Task B — Investigate the api / core structure (decides Phase 2 shape)

Phase 2 of the scaffold is "generate the thin `*-api` shell + its `*-core` lib". Before
building it we need to know how the real estate is actually structured. Answer these
from the mirror, citing real paths (`repo/path`) as evidence — do not guess:

1. Pick **5–8 representative business services** (NOT shared libs like `api-common` /
   `api-parent`). For each, list its thin shell repo (`*-api`) and its matching core
   repo (`*-core`). Are they **two independent git repos** (each with its own `pom.xml`,
   each a top-level dir under `mirror/`), or **multiple Maven modules inside one repo**?

2. What is the **naming relationship** between a shell and its core, and is it stable?
   (e.g. service `mc-hk-hase-ingress-api` ↔ core `mc-hk-hase-api-ingress-core`.)

3. Does a service's `*-api` shell `pom.xml` depend on **its own dedicated `*-core`**, or
   mainly on **existing shared libs** (`api-common` / `api-domain` / `api-dao` …)? In
   other words: when a NEW service is created, does a new `*-core` repo typically get
   created alongside it, or does the new shell reuse existing cores?

4. Give a **real file/directory listing** of a typical shell repo and a typical core
   repo (one example each, actual paths), so we can see what each half contains.

## Deliverable

Write your report to `docs/specs/scaffolding-feedback.md` **and** print the same content
to stdout, with two sections — **"Task A results"** and **"Task B findings"** — each
backed by real `repo/path` citations. **Do not commit or push** (you can't); the
maintainer will collect the file / output and push it back. That report is what the next
decision (how to build Phase 2) will be made from.
