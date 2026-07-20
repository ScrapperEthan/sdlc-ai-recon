# Spec: Producer coverage for the async message map

**Status:** recon DONE (2026-07-17) · build v1 DONE (2026-07-17, Claude) · Part 3 real-mirror verify DONE (2026-07-17, internal Codex — **NEEDS ITERATION**: producer *identity* accurate but 0/126 added records had a resolved destination) · build v2 DONE (2026-07-18, RUNBOOK-42, Claude) · Part 5 re-verify DONE (2026-07-20, internal Codex — **FAILED gate**: v1/v2 `message_edges.csv` byte-identical on the real mirror, zero new resolutions) · **build v3 DONE (2026-07-20, Claude — Lombok `@Getter`/`@Data`/`@ConfigurationProperties` synthesis + method-declaration guard + multi-arg destination search, 17 producer tests / 156 total)** · re-verify = NEXT (internal Codex, real mirror — Part 8, same acceptance gate) · **Owner split:** recon = internal Codex (box), build = Claude (this repo), verify = internal Codex (box)
**Motivates:** the flagship [impact-notification] use case — upstream tracing ("who PUBLISHES to this topic / feeds this channel") is currently blind.

## Problem

`make_message_map.py` emits `index/message_edges.csv` (producer_repo, destination, consumer_repo, …).
On the real mirror it finds ~699 consumer edges but only ~10 producer edges. The upstream half of
every impact/outage answer is therefore near-empty.

**Root cause (confirmed from the code, not a regex typo).** The estate is *config-driven for
consumers but programmatic for producers*:

- Consumers declare destinations as literals in `application.yml`
  (`consumerInformationList: … topicName:`), which the scanner sees directly → high recall.
- Producers publish in Java where the destination is **resolved at runtime** — a `getTopicName(channel)`
  builder, a constant/enum reference, a config-key lookup, or the repo-naming convention itself
  (see [mdc-messaging-architecture]: repo name encodes channel+vendor; Topic/Delivery-Job repos).
  The destination literal is rarely on the same line (or within the 5-line window) as the
  `.send()` / `convertAndSend` call, so `_role()`'s produce markers almost never co-occur with a
  destination match → near-zero producer recall.

This is an **architecture-shaped discovery problem**, so it needs a recon pass on the real source
before any harvester can be written well. Claude cannot see the mirror; only the box can.

## Division of labour (why it's split this way)

The box is air-gapped: code written there cannot be pushed out. So **all durable code is written
in this repo by Claude and pushed IN; the box RUNS it and relays findings OUT as text.** The
generated `index/message_edges.csv` is gitignored — it is *meant* to live only on the box, so the
box producing it is not a loss.

---

## Part 1 — RECON (internal Codex on the box) → relay findings to Claude in-session

Goal: characterise how producers actually name and send to destinations, with ~15–25 concrete
samples, so Claude can write precise extractors. **Do not commit findings** — they contain real
repo names/paths; relay them to Claude in-session (paste/photo per the usual flow). Claude
generalises them into name-free patterns in code.

Run over the mirror (read-only, stdlib/grep only). For each question, return a short table of
`repo/path:line` + the surrounding 1–2 lines:

1. **Send call sites.** Where are messages actually published? Grep for:
   `convertAndSend`, `jmsTemplate.send`, `kafkaTemplate.send`, `\.send\(`, `producer.send`,
   `publish`, `emit`, `MessageProducer`, `@Output`, `StreamBridge`, `outputChannel`.
   → Which idioms dominate? Roughly how many repos contain at least one?

2. **How is the destination argument built?** For ~15 of those call sites, what is the first arg?
   Classify each as: (a) **string literal**, (b) **constant/enum ref** (e.g. `Topics.SMS_REQ`),
   (c) **builder/method** (e.g. `getTopicName(channel)`, `resolveTopic(...)`), (d) **config-key /
   `@Value` / yml property**, (e) **injected/framework-bound** (Spring Cloud Stream binding name).
   → Give the distribution (how many of each).

3. **Producer wrapper classes.** Are there common wrapper/base classes or annotations that mark a
   producer (names like `*Producer`, `*Publisher`, `*DeliveryJob`, `*Sender`, `*Emitter`, or a
   shared base in `mc-hk-hase-api-*`)? List the class names + which repo defines each.

4. **Topic-name resolution.** For the builder/config cases (2c/2d), where does the actual topic
   string come from — a constants file, an enum, an `application.yml` producer block, or a
   convention derived from the repo name? Show 3–5 examples of the resolved literal + its source
   line.

5. **Delivery-Job / Topic repos.** Per [mdc-messaging-architecture], do repo *names* encode the
   producer→channel/topic mapping (e.g. `*-topic-*`, `*-delivery-job`, vendor tokens)? List ~10
   such repo names (tokens only if names are sensitive) and whether their config declares the
   destination they own.

**Relay-out artifact:** answers to 1–5 as text (a few tables). That's the whole handoff.

---

## Part 2 — BUILD (Claude, this repo) — now grounded in the recon findings

The recon confirmed the hypothesis with numbers: **0% of destinations are string literals**;
45% config/`@Value`/YAML, 32% builder/method-return, 9% constant/enum, 14% injected/framework-bound.
So a keyword scan is the wrong tool — the extractor must be **type-aware + wrapper-aware + must
resolve the destination through a small data-flow ladder.** Build order (each with synthetic
fixtures + tests, no real repo names in-repo):

### 2a. Framework signature registry (recon Q1/§8.1)
A small table of `receiver_type → send method(s) → where the destination lives`. From the recon:

| receiver / wrapper | send method(s) | destination position |
|---|---|---|
| `JmsTemplate` | `send`, `convertAndSend` | arg0, else receiver default destination |
| `KafkaTemplate` | `send` | arg0, or `topic` inside a `ProducerRecord` |
| `MessageProducer` (JMS) | `send` | injected `Destination` / on the message |
| SNS client | `publish` | request/builder (topic ARN) |
| RocketMQ | `send` | message `topic`/`tags`, builder/constructor config |
| in-house wrapper | `publishMessage`, `publishMessageForEventModel` | `EventConfig` / `topicName` |

Guard generic `.send(`/`publish(` with the receiver type (recon §3.2): only count it when the
receiver resolves to a known framework/wrapper type — never on keyword alone.

### 2b. Wrapper-aware recognition (recon Q3/§5) — the highest-leverage step
Producers overwhelmingly go through ~26 wrapper/base classes across ~22 repos. Recognise a class as
a producer when it extends/implements one of the known bases and map its business-level call to a
producer edge. Seed the base-class set from the recon (kept as config, not hard-coded literals):
`AbstractEventProducer`, `AbstractKafkaProducerService`, `EventProducer`/`IEBProducer`,
`RocketmqEventProducer`, `MailProducer`, `SqsProducer`, `EBKafkaProducer`,
`EBKafkaTransactionProducer`, `EBProducer`, `Sender`/`KafkaSender`/`DelayQueueSender`, and the
`*EventService` / `*SendService` families. Resolve each wrapper to the framework API it calls
internally so business callers count as producers.

### 2c. Destination resolution ladder (recon Q2/Q4/§4.3)
Resolve `destination_expression` → `resolved_destination` by trying, in order, and recording which
rung succeeded in `routing_source`: `literal` → `constant`/`enum` (look up the value in the same
repo) → `config`/`@Value`/`yaml` (read the property) → `builder`/`method-return` (e.g.
`EventConfig.getTopicName()`; attribute to the channel/topic it resolves to) → `injected`/
`framework-bound` (binder default). Record fallbacks too (recon §6.4: primary + fallback topic).
Anything unresolved is **kept, not dropped**, as `runtime-unresolved` / `framework-default` /
`config-unresolved`.

### 2d. Output schema (recon §8.2) — extend `index/message_edges.csv` additively
Add trailing columns so existing readers (`retriever/messages.py`, `unified_impact`) keep working:
`producer_type, producer_symbol, call_site, destination_expression, destination_kind, routing_source,
confidence, resolution_status` (existing `producer_repo, destination, consumer_repo, routing_source,
evidence` stay in place). `confidence` is tied to the resolution rung (literal/constant high →
runtime-unresolved low), **never to keyword hit count** (recon §8.3).

### 2e. Delivery-job repos — do NOT infer direction from the repo name (recon Q5/§7)
110 delivery-job repos host producer AND consumer config together. Parse both `producer.enabled`
and `consumer.enabled` and bind each `topicName` to the specific handler/sender class before
emitting an edge. A repo-name/convention match alone yields at most a `confidence: low,
routing_source: repo-convention` hint, never a confirmed producer edge.

Deliverable: code + fixtures + tests in `make_message_map.py`, pushed IN by the user. **Symbol
identity across bundles must be `repo+package+class+method`, not bare method name** (recon §9.3) —
`publishMessage`/`getTopicName` recur in 20+ bundles and will cross-pollinate if keyed by name only.

---

## Part 3 — VERIFY (internal Codex on the box) — after push

1. Run `python make_message_map.py` on the full mirror.
2. Report the coverage delta: producer edges before (~10) → after; how many via each
   `routing_source`; how many repos gained ≥1 producer edge.
3. Spot-check 10 new producer edges against the cited `path:line` — are they real sends to that
   destination? Report the false-positive rate.
4. Sanity: consumer edge count must not regress; total runtime acceptable.

Relay the numbers + the spot-check table out to Claude; iterate Part 2 if precision is low.

---

## Part 3 — RESULT (internal Codex, real mirror, 2026-07-17) → NEEDS ITERATION

Ran read-only over the full 457-dir HASE_MDC mirror (outputs under `.tmp/`, live index untouched):

- **Producer identity is accurate.** Spot-check false-positive rate **0/10**; consumer coverage did
  not regress (699 rows). Wrapper/signature recognition works.
- **But destinations don't resolve.** 126 producer records added, **0 with a resolved destination**.
  Routing source of the added rows: `runtime-unresolved` 76, `wrapper` 47, `builder` 3, resolved
  literal/constant/config **0**. Usable `topic/queue -> producer` edges: **10 -> 10** (no change).
- **Root cause = a data-flow gap, not identity.** The send arg is a chained config getter
  (`config.getQueue()`), a nested getter into a YAML/property value, or an `EventConfig` /
  `getTopicName()` whose value lives in another method — hops the v1 single-line ladder never crossed.

Verdict: **do not promote the v1 CSV into the live index.** The 126 rows are useful producer
*candidates*, not complete message edges; they can't answer `who_produces(<topic>)` yet.

---

## Part 4 — RESOLVE (build v2, RUNBOOK-42, Claude — DONE 2026-07-18)

Addresses the Part-3 gap directly. Producer *identity* (wrapper + guarded send sites) was already
right, so v2 leaves it alone and adds a **per-repo destination resolver** — `RepoIndex` in
`producer_extract.py`, built once per repo from its `.java` + `.yml`/`.properties`, still stdlib and
read-only:

1. **First-arg extraction.** The send's first top-level argument is parsed (`_first_arg`) instead of
   the old lone-trailing-identifier heuristic, so `send(dest, payload)` resolves on `dest`.
2. **Cross-file constants.** `SMS_TOPIC` / `Topics.SMS_TOPIC` resolve against a repo-wide constant
   table, not just same-file (recon bucket: constant/enum 9%).
3. **`@Value` → yaml/properties.** `@Value("${notification.order.topic}")` fields resolve the key
   through a stdlib yaml flattener + `.properties` reader (recon bucket: config/@Value/YAML 45%).
4. **Getters → their value.** `eventConfig.getTopicName()` / `config.getQueue()` resolve by reading
   the getter body and following its `return` (constant, `@Value` field, or another getter — 2-pass
   fixpoint for nested getters) (recon bucket: builder/method-return 32%).
5. **Candidates are still kept, not dropped.** A getter/`@Value` we recognise but can't value stays a
   `builder`/`config` candidate with `resolution_status: unresolved` (better signal than
   `runtime-unresolved`); truly opaque sends stay `runtime-unresolved`.
6. **Metrics separate candidates from edges** (Part-3 recommendation 3/4). `make_message_map` stdout
   now prints `producer_records_extracted` (candidates) vs `usable_topic_producer_edges`
   (who_produces-answerable) plus a per-`routing_source` resolved/total breakdown, so a row count can
   never be mistaken for edge coverage.

Tests: 5 new resolution cases (`@Value`→yaml, getter→constant, chained config getter→properties,
cross-file qualified constant, unresolved-config candidate) — 13 producer tests, 117 total, all pass
on synthetic fixtures (no real repo names in-repo).

---

## Part 5 — RE-VERIFY (internal Codex, real mirror) — WITH AN ACCEPTANCE GATE

Re-run `python make_message_map.py` over the full mirror (outputs under `.tmp/`, live index
untouched) and relay:

1. **Resolved-destination delta.** How many added records now have `resolution_status: resolved`,
   broken down by `routing_source` (the new stdout breakdown gives this directly).
2. **`usable_topic_producer_edges`** — the who_produces-answerable count. Must rise above 10.
3. **Spot-check 10 newly-resolved edges** against their cited `path:line`: is the resolved
   destination the real topic/queue that send publishes to? Report the resolution false-positive rate.
4. **No regression:** consumer edges stay ~699; identity false-positive rate stays ~0.

**Acceptance gate (Part-3 recommendation 5) — promote to the live index only if ALL hold:**
- resolved-destination delta **> 0** (v1 was 0), and
- `usable_topic_producer_edges` **> 10** (a measurable `who_produces(topic)` improvement), and
- resolution spot-check false-positive rate is low (≤ ~1/10), and
- consumer count does not regress.

If the gate passes, promote the CSV; otherwise relay the failing spot-checks so Claude can extend the
resolver (likely candidates: deeper nested getters, enum-backed topic registries, or a CodeGraph
pass for cross-repo `getTopicName` chains the repo-local resolver can't reach).

---

## Part 6 — RESULT (internal Codex, real mirror, 2026-07-20) → GATE FAILED, root causes found

Re-ran Part 5 on the full 457-dir mirror (`.tmp/runbook42-real/`, live index untouched, ~49s):

- **v1 and v2 produced a byte-identical `message_edges.csv`** (SHA-256 match) — v2's resolver added
  **zero** new resolutions on real code. `usable_topic_producer_edges` stayed at 10; all three
  `routing_source` buckets were 0-resolved (`builder` 0/3, `runtime-unresolved` 0/76, `wrapper` 0/47).
  No spot-check was possible — there was nothing new to check. Consumer edges held at 699 → 699 (no
  regression); producer-identity false-positive rate on the 10 still-unresolved candidates spot-checked
  was 0/10 (identity remains fine — this is purely a resolution-ladder gap).
- **Root causes, found by manually tracing 3 real unresolved getters to their actual value:**
  1. **Lombok.** All 3 traced getters (`getQueue() -> q_csl_tracking`, `-> q_htcl_tracking`,
     `-> q_pfp_tracking`) were `@Getter`/`@ConfigurationProperties` fields with **no getter body in
     source** — Lombok generates them at build time. v2's `_GETTER_DEF_RE` only matches an explicit
     `get...() { ... return ...; }`, so it never saw these at all.
  2. **44 wrapper-call misses**: the destination sits in a later argument (`send(payload,
     eventConfig)`), not the first — v2's `_first_arg`-only extraction can't reach it.
  3. **9 false records**: a wrapper's own method *declaration* reusing a trusted method name (e.g.
     `public void publishMessage(String topic, byte[] payload) {`) was being counted as a call site,
     since the confirmed-receiver guard only applies to the generic `send`/`publish` names, not the
     trusted ones.
  4. Remaining ~20 lower-yield gaps not addressed this round: JMS default-destination / raw
     `Destination`-typed fields (19), nested/generated getters (3), an SNS request with an embedded
     destination (1).

**Verdict: do not promote v1/v2's CSV.** Producer identity is solid; the resolution ladder needs the
Lombok gap closed before this workstream can move the needle.

---

## Part 7 — RESOLVE (build v3, RUNBOOK-42 cont'd, Claude — DONE 2026-07-20)

Targets the three root causes above directly (the ~20 lower-yield cases in Part 6 item 4 are left for
a future round):

1. **Lombok `@Getter`/`@Data`/`@ConfigurationProperties` synthesis.** `_build_index` now walks each
   class body (brace-matched) and, when the class or a field carries `@Getter`/`@Data`, synthesizes a
   `getXxx` -> field mapping fed into the same getter-resolution fixpoint used for explicit-body
   getters — no new resolution path, just a new source for it. A field's value resolves through
   whichever ladder rung already applies to it: its own initializer, an `@Value` binding, or (new) a
   `@ConfigurationProperties(prefix=...)` binding via `camelCase` -> `kebab-case` relaxed binding
   against the repo's yaml/properties.
2. **Method-declaration guard.** A bare (no-receiver) call to a trusted method name is now checked
   against `_looks_like_declaration` — if the arg-list's closing paren is followed by `{` (a method
   body), it's a declaration, not a call, and is skipped.
3. **Multi-arg destination search.** `_send_records` now tries every top-level argument in call order
   (via new `_all_args`) instead of only the first, taking the first one that actually resolves.

4 new tests reproduce the shapes Codex traced by hand (Lombok+`@Value`, Lombok+`@ConfigurationProperties`,
a redeclared trusted-method-name wrapper method, a second-argument destination) — 17 producer tests,
156 total, all pass on synthetic fixtures (no real repo/topic names in-repo).

---

## Part 8 — RE-VERIFY (internal Codex, real mirror) — SAME ACCEPTANCE GATE AS PART 5

Re-run `python make_message_map.py` over the full mirror (outputs under `.tmp/`, live index
untouched) and relay the same four items as Part 5 (resolved-destination delta by `routing_source`,
`usable_topic_producer_edges`, a 10-edge spot-check false-positive rate, consumer/identity
regression check) against the **same acceptance gate**. Additionally:

- Confirm the 3 getters Codex traced by hand in Part 6 (`q_csl_tracking`/`q_htcl_tracking`/
  `q_pfp_tracking`, described generically — do not paste real repo names back into this file) now
  resolve.
- Confirm `message_edges.csv` is no longer byte-identical to the v1/v2 run.

If the gate still fails, relay which of the four Part-6 root causes (Lombok, second-arg, false
declarations) remain un-fixed vs. which of the ~20 lower-yield gaps (JMS default-destination, nested
getters, SNS-embedded destination) are now the bottleneck, so the next round can be scoped precisely.

## Guardrails

- stdlib only, read-only over the mirror (never writes to `mirror/`); output stays under `index/`
  (gitignored) — see [sdlc-hard-constraints].
- No real repo names, coordinates, or topic strings in anything committed to this repo — recon
  findings are relayed in-session and generalised into name-free code + synthetic fixtures.
- `message_edges.csv` schema stays additive (append `confidence`); `retriever/messages.py` and
  `unified_impact` keep working with or without the new column.
