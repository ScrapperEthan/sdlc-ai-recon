# Spec: Producer coverage for the async message map

**Status:** recon DONE (2026-07-17, internal Codex) · **build DONE (2026-07-17, Claude — `producer_extract.py` + wired into `make_message_map.py`, 8 tests)** · verify = NEXT (internal Codex, real mirror — Part 3) · **Owner split:** recon = internal Codex (box), build = Claude (this repo), verify = internal Codex (box)
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

## Guardrails

- stdlib only, read-only over the mirror (never writes to `mirror/`); output stays under `index/`
  (gitignored) — see [sdlc-hard-constraints].
- No real repo names, coordinates, or topic strings in anything committed to this repo — recon
  findings are relayed in-session and generalised into name-free code + synthetic fixtures.
- `message_edges.csv` schema stays additive (append `confidence`); `retriever/messages.py` and
  `unified_impact` keep working with or without the new column.
