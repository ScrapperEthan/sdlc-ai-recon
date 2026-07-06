# Spec: vertical slice Phase 2 — intent → retrieval-grounded target → templated change → verify → diff

**Audience:** external Codex IMPLEMENTS (build & test against fixtures — no `mirror/`, no
retriever indexes, no LLM, no Maven on its box); internal Codex RUNS it against the real
`mirror/` + real retriever + real Maven. Read `PROJECT-STATE.md`, `docs/specs/vertical-slice.md`
(Phase 1, now CLOSED), and the retriever modules first. This is the **next capability after
the Phase 1 vertical slice**: stop hardcoding *which service* and *where*, and instead let the
retrieval layer (the moat) **locate** the change from a short ask — with a reviewable, cited
rationale — then reuse the Phase-1 verify loop unchanged.

## Why

Phase 1 proved the *verify half* (generate → compile+test → diff) but the change is fully
hardcoded: `change.add_endpoint <service-repo> --path /status`. The operator still has to know
the exact repo. Phase 2 connects **Step 1 retrieval (the durable moat)** to the generator: an
ask names *what* and *roughly where*, and the tool resolves it to a concrete repo + controller
**grounded in real code, with `repo/path:line` citations a human can verify.** The credible new
capability is **grounded targeting**, not fuzzy natural-language codegen — so we keep the change
*kind* templated and put the intelligence in *locate + justify*.

## Scope discipline (what this is and is NOT)

- **IS:** ask → structured `ChangeRequest` → **retrieval-grounded target selection** (repo +
  controller) with a cited rationale → the existing `change.add_endpoint` core → existing
  verify + diff. Plus one new reviewable artifact: `TARGET_RESOLUTION.md`.
- **IS NOT:** free-form English → arbitrary code. The change *content* stays the Phase-1
  templated GET endpoint. No new change *kinds* (message listener / DAO / config) yet.
- **The NL parser ships rule-based and injectable.** A real LLM parser is a later drop-in the
  internal box can inject; do **not** require an LLM to build or test this.

## Guardrails (do NOT violate — same as Phase 1, plus targeting rules)

- **Read-only on `mirror/`.** Resolution only *reads* the mirror/indexes; the change still goes
  to a `scratch/` copy via the Phase-1 core. Never write under `mirror/` or `hase-mc`.
- **Scratch only.** All output (the copy, diff, build log, resolution rationale) under `scratch/`.
- **Stdlib for our tooling** (+ `mvn` as the one external process, internal box only).
- **Never act on an unresolved or ambiguous target.** 0 candidates or >1 comparably-strong
  candidates ⇒ **stop, write `TARGET_RESOLUTION.md` listing the candidates and why it can't
  decide, exit non-zero. Never silently pick.**
- **No hallucinated locations.** Every `repo/path:line` in the rationale must pass
  `retriever.citations.verify` (the line must actually exist). Drop any that don't.

## The contract: `ChangeRequest` (the seam between parse and act)

A small stdlib dataclass in `change/intent.py` — this is the swappable boundary so the NL
front-end can be replaced without touching resolution or application:

```python
@dataclass(frozen=True)
class ChangeRequest:
    kind: str            # only "add_endpoint" in Phase 2 (validate against a small enum)
    target_hint: str     # e.g. "ingress service", "the tracking api"
    path: str            # e.g. "/status"  (validated by change.add_endpoint._endpoint_path)
    method: str | None = None
```

`parse_intent(text: str, parser: Callable | None = None) -> ChangeRequest`:
- Default `parser` is **rule-based** stdlib: extract the endpoint path (`/[\w/…]`), confirm the
  kind is an endpoint (keywords: endpoint/route/mapping/GET), take the remaining noun phrase as
  `target_hint`. If it can't extract a path or a hint, raise `ValueError` (don't guess).
- `parser` is injectable so the internal box can later pass an LLM-backed parser returning the
  same `ChangeRequest`. Tests inject a trivial fake parser AND exercise the rule-based default.

## Target resolution (the moat doing the work): `change/locate.py`

`resolve_target(request, mirror="mirror", resolver=None) -> TargetResolution`

```python
@dataclass(frozen=True)
class TargetResolution:
    repo: str                      # chosen service repo under mirror/
    controller_path: str           # repo-relative path to the @RestController to edit
    candidates: list[Candidate]    # all considered, with scores
    rationale: list[str]           # human sentences, each backed by a verified repo/path:line
```

Resolution algorithm (reuse existing code — import, don't copy):
1. **Candidate repos from the hint.** Read `index/REPOMAP.md` (produced by `make_repomap.py`:
   repo name + purpose + entry-point annotations) and/or `retriever.code.search_code` to find
   repos whose name/purpose match `target_hint` tokens **and** that expose a `@RestController`.
   Score by token overlap + entry-point presence.
2. **Pick the controller** in the top repo by reusing `change.add_endpoint._find_controllers`
   (prefers a class under a `resource`/`controller` package).
3. **Justify.** Build `rationale` sentences citing the REPOMAP line and the controller line
   (`repo/path:line`); **filter every citation through `retriever.citations.verify`**.
4. **Decide or refuse.** Exactly one clearly-top candidate ⇒ return it. Zero, or a near-tie
   between the top two (define a small margin) ⇒ raise `AmbiguousTarget` carrying the candidate
   list (the CLI turns this into `TARGET_RESOLUTION.md` + non-zero exit).
- `resolver` is injectable: fixtures pass a fake that reads a fixture repomap / fixture mirror,
  so the suite runs with **no real mirror, no retriever indexes**.

Write `TARGET_RESOLUTION.md` under the scratch copy (or the out-dir on refusal): chosen
repo + controller, the ranked candidates with scores, and the cited rationale.

## Orchestration CLI: `change/from_intent.py`

```
python -m change.from_intent "add a /status endpoint to the ingress service" \
    [--mirror mirror] [--out-dir scratch] [--explain-only] [--skip-build] [--force]
```
Flow: `parse_intent` → `resolve_target` → (unless `--explain-only`) call the **existing**
`change.add_endpoint.add_endpoint(service_repo=resolution.repo, path=request.path,
method=request.method, mirror=…, out_dir=…, skip_build=…)` → existing verify + diff.
Emit, under `scratch/<repo>-change/`: `TARGET_RESOLUTION.md` **plus** the Phase-1
`CHANGE_DIFF.md` + `BUILD_RESULT.md`.

- `--explain-only`: resolve + write `TARGET_RESOLUTION.md` and STOP (human sanity-checks the
  target before any code is generated). No copy, no build.
- Reuse Phase-1 exit-code semantics (build failure ⇒ non-zero, artifacts still written). An
  ambiguous/failed resolution ⇒ non-zero, `TARGET_RESOLUTION.md` written, no change applied.

## Where

- New: `change/intent.py` (`ChangeRequest`, `parse_intent`), `change/locate.py`
  (`TargetResolution`, `Candidate`, `resolve_target`, `AmbiguousTarget`),
  `change/from_intent.py` (CLI), `change/tests/test_intent.py`, `change/tests/test_locate.py`,
  `change/tests/test_from_intent.py`.
- **Reuse (import, don't duplicate):** `change.add_endpoint` (core apply + verify + diff),
  `change.add_endpoint._find_controllers`, `retriever.code.search_code`,
  `retriever.citations.verify`, `scaffold.reference._package_from_java`.
- `.gitignore` already covers `scratch/`.

## Fixtures + tests (external Codex — no mirror / no retriever / no Maven)

- Extend `change/tests/fixtures/` to a **multi-repo** fixture mirror so resolution must *choose*:
  keep `mc-hk-hase-fixture-api` (has `IngressResource` `@RestController`) and add a second repo
  (e.g. `mc-hk-hase-other-api`) with a different controller, plus a tiny fixture `REPOMAP.md`.
- Inject fakes for the parser and the resolver's mirror/repomap reads; inject the Phase-1
  `runner=` for the mocked build.
- Tests assert:
  1. Rule-based `parse_intent` extracts `path` + `target_hint`; a garbled ask raises `ValueError`.
  2. `resolve_target` picks the **correct** repo from the hint and cites **real fixture lines**
     (rationale citations resolve; a bad line is dropped).
  3. An ambiguous hint (matches two repos comparably) ⇒ `AmbiguousTarget`, `TARGET_RESOLUTION.md`
     lists both candidates, CLI exits non-zero, **no scratch copy created**.
  4. `--explain-only` writes `TARGET_RESOLUTION.md` and applies **no** change.
  5. End-to-end `from_intent` (with fakes + mocked build) writes `TARGET_RESOLUTION.md` +
     `CHANGE_DIFF.md` + `BUILD_RESULT.md`, and the fixture source under the mirror is untouched.
  6. Path-escape / invalid path still rejected (delegated to Phase-1 validators).

## Done when (fixtures)

1. `python -m change.from_intent "<ask>"` against the fixture mirror resolves the right repo,
   emits a cited `TARGET_RESOLUTION.md`, applies the templated endpoint, and emits
   `CHANGE_DIFF.md` + `BUILD_RESULT.md` (mocked build). Unit tests pass.
2. Ambiguous/failed resolution refuses cleanly (artifact + non-zero, no change).
3. Fixture mirror is never modified.

## Verification steps (internal Codex — real mirror + real retriever + real Maven)

```
python -m change.from_intent "add a /status endpoint to the ingress service" --explain-only
python -m change.from_intent "add a /status endpoint to the ingress service" --out-dir scratch
```
Confirm: `--explain-only` names `mc-hk-hase-ingress-api` + its `IngressResource` with a
**citation-backed** rationale (lines verify against the real mirror); the full run then reuses
the Phase-1 loop — `mvn test` PASS, clean `CHANGE_DIFF.md`, `mirror/` untouched
(`MIRROR_HASH_UNCHANGED=True`). Try a deliberately vague ask (e.g. "add an endpoint to the api")
and confirm it **refuses** with a candidate list rather than guessing. Report the artifacts.

## Explicitly deferred (NOT Phase 2)

- New change *kinds*: message listener (`@JmsListener`/`@KafkaListener`), DAO/DB, config — the
  REPOMAP already indexes these annotations, so they're the natural Phase 3, but out of scope here.
- Free-form change *content* generation; multi-file / cross-repo / `*-core` edits.
- A real LLM intent parser (ship rule-based; LLM is an injected upgrade).
- Auto-committing / PR-raising — output stays a scratch diff + rationale for a human.

## Needs a decision (maintainer)

- **Resolution inputs:** is `index/REPOMAP.md` present & current on the box, or should
  `resolve_target` call `make_repomap.py` / `search_code` live? (Prefer reading REPOMAP if fresh.)
- **Ambiguity margin:** how close is "too close to decide" (e.g. top score within X% of #2)?
- **Confirm the house REST convention** on the real reference service still matches Phase 1
  (base path, response type, new method vs new `*Resource`).
