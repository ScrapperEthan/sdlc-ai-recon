# Spec: Producer coverage for the async message map

**Status:** proposed (2026-07-17) · **Owner split:** recon = internal Codex (box), build = Claude (this repo), verify = internal Codex (box)
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

## Part 2 — BUILD (Claude, this repo) — after the recon comes back

From the recon, Claude extends producer detection in `make_message_map.py` (and adds tests with
synthetic fixtures — no real names). Likely shape, to be finalised from findings:

- **Widen the produce evidence beyond one line.** Detect a send call site, then resolve its
  destination by following the arg: literal → use it; constant/enum → look up the constant's value
  in the same repo; builder → attribute the producer to the channel/topic the builder resolves to;
  config-key → read the yml/properties value. Emit `producer_repo` with `routing_source` recording
  *how* it was resolved (`literal` / `constant` / `builder` / `config` / `repo-convention`) and a
  `confidence` field (Codex review §10 asked for this).
- **Repo-convention fallback.** When a repo is a Topic/Delivery-Job repo (per repo_tags /
  naming), treat it as a producer for its owned destination even without a literal send match,
  tagged `confidence: low, routing_source: repo-convention`.
- Keep the CSV schema backward-compatible; add `confidence` as a new trailing column so existing
  consumers of the file don't break.

Deliverable: code + fixtures + tests in this repo, pushed IN by the user.

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
