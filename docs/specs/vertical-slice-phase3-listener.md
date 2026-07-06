# Spec: vertical slice Phase 3 — a second change kind: add a message listener

**Audience:** external Codex IMPLEMENTS (build & test against fixtures — no `mirror/`, no
retriever indexes, no Maven); internal Codex RUNS it against the real `mirror/` + real Maven
(RUNBOOK 5, to follow). Read `PROJECT-STATE.md`, `docs/specs/vertical-slice-phase2-intent.md`
(Phase 2, verified), and `RUNBOOK-3-message-map.md` first.

This is the **next capability after Phase 2**. Phase 1+2 proved the loop *ask → locate →
templated change → compile+test → diff* on ONE change kind (add a GET endpoint). Phase 3
proves the loop **generalizes to a second, HASE-core change kind: adding a message listener**
(`@JmsListener`) to a consumer service. Two verified kinds is the point — it turns "a clever
one-off" into "a general capability", which is the PoC result we need to demonstrate.

## Why a listener (not another endpoint)

HASE is **event-driven** — SQS-via-JMS message listeners are the system's main communication
mode (see RUNBOOK 3). A change that adds a listener, grounded in the **message map**
(`who_consumes`/`who_produces`), exercises the hardest-won part of the retrieval moat (async
wiring, which CodeGraph can't see). It's the most credible "it understands our real system"
demo. Keep the listener *body* trivial (a no-op/log handler) — the value is in **locating the
right consumer service + grounding the change in real routing**, not in business logic.

## Scope discipline (what this is and is NOT)

- **IS:** a new change kind `add_listener` that (a) adds a `@JmsListener(destination="…")`
  method to an existing consumer class in the house style (or creates a `*Listener` class next
  to existing ones if none fits), (b) generates a matching test, (c) runs through the **same
  Phase-1 verify loop and Phase-2 intent/locate front-end**, (d) grounds the target + rationale
  in the message map.
- **IS NOT:** producer-side changes, Kafka/Rabbit variants (JMS only now), real handler logic,
  DAO/DB or config kinds, cross-repo or `*-core` edits. No new NL sophistication — extend the
  rule-based parser minimally.

## Guardrails (unchanged from Phase 1/2 — do NOT violate)

- **Read-only `mirror/`.** Change goes to a `scratch/` copy only. Never write `mirror/`/`hase-mc`.
- **Scratch only.** All output under `scratch/`.
- **Stdlib** for our tooling (+ `mvn` as the one external process, internal box only).
- **Refuse to guess.** Ambiguous/absent target ⇒ stop, write `TARGET_RESOLUTION.md`, exit
  non-zero, apply nothing (reuse Phase-2 `AmbiguousTarget`).
- **No hallucinated locations.** Every `repo/path:line` (including message-map evidence) must
  pass `retriever.citations.verify`; drop what doesn't.

## Reuse, don't duplicate (the key architectural ask)

`change/add_endpoint.py` already contains the shared orchestration: `_copy_service` → apply →
`run_maven_tests` → `_write_change_diff` / `_write_build_result`. **Factor the shared
copy→apply→verify→diff→artifacts flow into a reusable core** (e.g. `change/apply.py` with an
`apply_change(service_repo, applier, …)` where `applier(project_dir) -> list[Path]` does the
kind-specific edits and returns touched files). Then:
- `add_endpoint` provides the endpoint applier (existing `_apply_change`),
- new `add_listener` provides a listener applier,
- both share copy/verify/diff/exit-code semantics. Do not copy-paste the orchestration.

## The contract: generalize `ChangeRequest`

`change/intent.py` `ChangeRequest` is endpoint-shaped (`path`, `method`). Generalize it so kinds
can carry their own params without a field explosion — either add an optional `destination:
str | None = None` field, or a small `params: Mapping` — and switch validation on `kind`:
- `SUPPORTED_KINDS = {"add_endpoint", "add_listener"}`.
- `add_endpoint` ⇒ require a valid `path` (existing `_endpoint_path`).
- `add_listener` ⇒ require a `destination` (a queue/topic name or a `${property.key}`
  placeholder); validate it's a safe single token / placeholder (no path escape, no quotes).

Extend the rule-based `_rule_based_parse` to recognise listener asks (keywords:
`listener`/`consume(r)`/`subscribe`/`queue`/`topic`/`JmsListener`) and extract the destination
(a `${…}` placeholder or a queue/topic identifier). Same swappable-parser boundary — an LLM
parser can still be injected later, returning the same contract. A garbled ask still raises.

## The change (templated, house-style): `change/add_listener.py`

Direct CLI (mirrors `add_endpoint`):
```
python -m change.add_listener <service-repo> --destination "${app.listener.foo.queue}" \
    [--method onFooMessage] [--mirror mirror] [--out-dir scratch] [--skip-build] [--force]
```
Applier steps:
1. **Locate host class:** find an existing `@JmsListener` host, or a `@Component`/`@Service`
   under a `listener`/`consumer` package (analogous to `_find_controllers`; add `_find_listeners`
   in the shared/locate code). If none fits, **create a new `*Listener` class** next to existing
   ones rather than guessing (mirror `_create_resource`).
2. **Insert** a `@JmsListener(destination = "<destination>")` method in the surrounding style
   (imports, annotations, indentation), body = trivial no-op/log consuming a `String message`.
   Add the `org.springframework.jms.annotation.JmsListener` import if absent.
3. **Generate/extend a test** consistent with the repo style: assert the new method carries
   `@JmsListener` with the expected `destination` (reflection, mirroring the endpoint test's
   `@GetMapping` value assertion).
4. Shared core handles copy → `mvn test` → `CHANGE_DIFF.md` + `BUILD_RESULT.md`.

## Intent + locate for listeners (the moat)

Wire `add_listener` into `change/from_intent.py` and extend `change/locate.py`:
- **Target repos** for a listener come from `index/REPOMAP.md` (already indexes
  `@JmsListener`/`@KafkaListener` entry points) + `search_code("@JmsListener")` + repo dirs,
  scored against the hint (reuse Phase-2 `_rank_candidates`; select on listener presence for
  `kind=add_listener` instead of `@RestController`).
- **Ground the destination in the message map:** call `retriever.messages.who_consumes(<dest>)`
  / `routes_for_repo(<repo>)`; add a cited rationale sentence like *"`<dest>` is already consumed
  by `<repo>` — evidence `<repo/path:line>`"* when the map has it. If the destination is unknown
  to the map, say so (partial) — **do not invent** producers/consumers. Filter every citation
  through `citations.verify`.
- Same refuse-to-guess: vague target (valid destination, ambiguous service) ⇒ `AmbiguousTarget`.

## Where

- New: `change/add_listener.py`, `change/apply.py` (extracted shared core),
  `change/tests/test_add_listener.py`, `change/tests/test_apply.py` (if the extraction warrants).
- Extend: `change/intent.py` (kind + destination + parser), `change/locate.py`
  (`_find_listeners`, listener candidate selection, message-map rationale),
  `change/from_intent.py` (route `add_listener`).
- **Reuse (import, don't copy):** the extracted `apply_change` core, `change.add_endpoint`
  helpers (`_copy_service`, `_write_change_diff`, `_endpoint_path`-style validators),
  `retriever.messages`, `retriever.citations.verify`, `retriever.code.search_code`.

## Fixtures + tests (external Codex — no mirror / no retriever / no Maven)

- Extend `change/tests/fixtures/`:
  - a consumer service **with** an existing `@JmsListener` host class (insertion-into-existing),
  - a consumer service **without** one (exercises the create-new-`*Listener` fallback),
  - a tiny fixture `index/message_edges.csv` so `who_consumes` returns a citable row,
  - fixture `REPOMAP.md` rows listing the listener services.
- Inject fakes for parser/resolver/message-map reads + the Phase-1 `runner=` mocked build.
- Tests assert:
  1. `parse_intent` extracts `destination` + `target_hint` for a listener ask; a listener ask
     with no destination raises; `add_endpoint` parsing still works (no regression).
  2. `add_listener` inserts `@JmsListener(destination="…")` into the existing host + adds a test;
     with no host, creates a `*Listener` class.
  3. `resolve_target` for `kind=add_listener` picks the right consumer repo and includes a
     **message-map** citation when available, omits it (not invents) when absent.
  4. Ambiguous target ⇒ `AmbiguousTarget`, `TARGET_RESOLUTION.md`, non-zero, no copy.
  5. End-to-end `from_intent` (fakes + mocked build) writes `TARGET_RESOLUTION.md` +
     `CHANGE_DIFF.md` + `BUILD_RESULT.md`; fixture source untouched.
  6. **All Phase 1/2 tests still pass** (the shared-core extraction must not regress add_endpoint).

## Done when (fixtures)

1. `python -m change.from_intent "add a JMS listener for the fooQueue to the <svc> service"`
   against the fixtures resolves the right consumer, applies a house-style `@JmsListener`, adds a
   test, and emits the three artifacts (mocked build). Unit tests pass, including all prior tests.
2. Ambiguous/failed resolution refuses cleanly. Fixture mirror never modified.

## Verification steps (internal Codex — real mirror + Maven; becomes RUNBOOK 5)

Pick a real consumer service that hosts listeners (a `*-job` / `*-tracking-*`; confirm below).
```
python -m change.from_intent "add a JMS listener for <real ${queue}> to the <svc> service" --explain-only
python -m change.from_intent "add a JMS listener for <real ${queue}> to the <svc> service" --out-dir scratch
```
Confirm: correct consumer repo + host class chosen, rationale cites REPOMAP + a message-map row
that verifies against the real `index/message_edges.csv`; `mvn test` PASS; `CHANGE_DIFF.md`
small/clean; `mirror/` untouched. A valid-destination-but-vague-service ask must **refuse**.

## Explicitly deferred (NOT Phase 3)

- Kafka/Rabbit/MQ variants (JMS only); real handler business logic; producer-side sends.
- DAO/DB and config change kinds (natural Phase 4 — REPOMAP indexes them too).
- Auto-commit / PR — output stays a scratch diff + rationale for a human.

## Needs a decision (maintainer)

- **Pilot consumer service** for the real run (which repo hosts a clean `@JmsListener` we can
  extend? likely a `*-tracking-job`). Confirm from the mirror / RUNBOOK 3 outputs.
- **House listener convention** (destination via `${property}` vs literal; method signature —
  `String` vs a typed payload; package `listener`/`consumer`) from a real reference listener.
- Is `index/message_edges.csv` present + current on the box (needed to ground the rationale)?
