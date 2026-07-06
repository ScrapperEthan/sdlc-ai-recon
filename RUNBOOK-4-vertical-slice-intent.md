# RUNBOOK 4 — verify vertical-slice Phase 2 (intent → change) on the real mirror

Goal: confirm that `change.from_intent` turns a plain-language ask into a **retrieval-grounded
target** (the right repo + controller, with a citation-backed rationale) and then reuses the
Phase-1 verify loop to produce a compiled, tested, reviewable change — all on the real
`mirror/` + real Maven, with production untouched.

This is the **internal Codex** job. Spec: `docs/specs/vertical-slice-phase2-intent.md`.
Phase 1 (the verify loop itself) is already proven — see `docs/specs/vertical-slice.md`.

**Read-only.** Resolution only *reads* `mirror/` and `index/`. The change goes to a `scratch/`
copy. Never modify or push anything under `mirror/` or `hase-mc`.

## Prereqs (confirm before running)

- **Latest code pulled** on the box (the Phase 2 commit: `change/intent.py`, `change/locate.py`,
  `change/from_intent.py`, the `retriever/citations.py` change).
- **Toolchain on PATH:** Zulu JDK 21 + Apache Maven 3.9.6 (Windows → `mvn.cmd`; `build.py`
  already resolves this).
- **`AD_PASS` set to the value Maven/Nexus actually needs** (not percent-encoded) so `mvn test`
  auth doesn't fail — otherwise a failure would look like a code problem when it's really creds.
- **`mirror/` present** and contains `mc-hk-hase-ingress-api`.

## Step 0 — Sanity: code arrived intact

```
python -m unittest change.tests.test_add_endpoint change.tests.test_build \
  change.tests.test_intent change.tests.test_locate change.tests.test_from_intent
```
Expect **19 tests OK**. If not, stop and report the failure — the delivery is broken.

## Step 1 — Refresh the repo map (grounds the targeting)

```
python make_repomap.py
```
Confirm `index/REPOMAP.md` exists and lists `mc-hk-hase-ingress-api`. (If REPOMAP is absent,
resolution still works via `search_code`, but the rationale loses its REPOMAP citation — so
regenerate it for a clean result.)

## Step 2 — Explain-only: does the MOAT pick the right target?

```
python -m change.from_intent "add a /status endpoint to the ingress service" --explain-only
```
Read `scratch/TARGET_RESOLUTION.md` and confirm:
- **Chosen repo** = `mc-hk-hase-ingress-api`.
- **Controller** = the existing `IngressResource` (`@RestController`).
- **Cited rationale** points at real lines (`index/REPOMAP.md:<n>` and the controller
  `repo/path:line`) — the lines must actually exist (they were verified via `citations.verify`).
- **No** scratch copy / no build happened (explain-only stops after resolution).

## Step 3 — Full run: does the verify loop still pass?

```
python -m change.from_intent "add a /status endpoint to the ingress service" --out-dir scratch
```
Under `scratch/mc-hk-hase-ingress-api-change/`, confirm:
- `TARGET_RESOLUTION.md` — same target as Step 2.
- `CHANGE_DIFF.md` — small & clean (endpoint added to `IngressResource.java` + a new test).
- `BUILD_RESULT.md` — **`mvn test` → PASS (exit 0)**.
- `mirror/` untouched — hash it before/after (`MIRROR_HASH_UNCHANGED=True`, ~3659 files).

## Step 4 — Refuse-to-guess: does it decline when unsure?

```
python -m change.from_intent "add an endpoint to the api" --explain-only
```
A deliberately vague hint should **refuse**: `TARGET_RESOLUTION.md` status `REFUSED`, a ranked
candidate list, **non-zero exit code**, and **no change applied**. (If it silently picks one,
that's a bug — report it.)

## Send back (paste this filled in)

```
Step 0  unit tests:            [ 19 OK / N failed → paste failures ]
Step 1  index/REPOMAP.md:      [ present & lists ingress-api? Y/N ]
Step 2  --explain-only target: [ repo = ______  controller = ______ ]
        rationale citations:   [ do the cited lines exist? Y/N; paste the rationale block ]
Step 3  CHANGE_DIFF.md:        [ files touched + line counts ]
        BUILD_RESULT.md:       [ mvn command + PASS/FAIL + exit code ]
        mirror untouched:      [ MIRROR_HASH_UNCHANGED = True/False ]
Step 4  vague ask:             [ REFUSED + non-zero + no change? Y/N ]
Anything surprising / errors:  [ ... ]
```

If Steps 2–4 all pass, Phase 2 is verified end-to-end and `PROJECT-STATE.md` should be updated
(Requirements-analysis / Code-generation front-end moves from ⚪ to a real beachhead). If
anything fails, paste the error + the relevant artifact and we fix the tool, not the mirror.
