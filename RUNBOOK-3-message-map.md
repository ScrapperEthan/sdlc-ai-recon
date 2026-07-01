# RUNBOOK 3 — build the message map (the async wiring CodeGraph can't see)

Goal: produce `index/message_edges.csv` — *who publishes to which queue/topic,
and who consumes it*. This fills CodeGraph's blind spot: it sees synchronous Java
calls, but NOT "service A publishes an event → queue → job B consumes it", which
is this system's main communication mode.

**Read-only.** Work over the local `./mirror/`. Never modify or push any repo.
All outputs go in `./index/`.

Prereqs: the `./mirror/` from RUNBOOK 2 (the 15-repo subset is enough to start;
add more `*-job` / `*-tracking-*` / `*-dispatch-*` repos if available).
`recon_out/` from RUNBOOK 1 is also available. The RECON-REPORT Section D already
lists many queues + consumers — reuse it as a starting point.

## Step 1 — Extract CONSUMERS (who receives)

Scan `./mirror/**` for message listeners:
- `@JmsListener` (AWS SQS via JMS — the common case here), `@KafkaListener`,
  `@RabbitListener`, IBM MQ listeners.
- For each: record the **destination property** (e.g.
  `${app.listener.otxBatchLetter.queue}`), the **repo**, and `file:line`.
- Resolve the property to a real queue/topic name by reading the
  `application.yml` / `*.properties` where that key is defined.

## Step 2 — Extract PRODUCERS (who sends)

Scan for send/publish calls:
- `EventProducerService` / `EventProducerManager.produce(topicName, event, mqType)`,
  SQS/SNS send, Kafka template send.
- Record the **repo**, the topic/queue variable, and `file:line`.
- The topic value is often **config / use-case driven** (not a literal in code).
  Where it is, record the **config key + file** and mark `routing_source=config`
  instead of guessing the destination.

## Step 3 — Resolve routing via CONFIG

Read the use-case / routing YAMLs that bind `use-case -> topic/queue`. Build a
small `use-case -> destination` lookup so producers whose topic comes from config
can be linked to a real destination where possible.

## Step 4 — Write outputs (in `./index/`)

1. `message_edges.csv` with columns:
   ```
   producer_repo,destination,consumer_repo,routing_source,evidence
   ```
   - `routing_source` = `annotation` | `config` | `partial`
   - `evidence` = `repo/path:line` (the proof)
   - If a producer or consumer side is unknown, leave it blank and set
     `routing_source=partial` — record what IS known rather than inventing the
     other side.
2. `message_destinations.md` — per queue/topic: producers, consumers, the config
   key, and a note wherever the link is only partial.

## Step 5 — Evaluate (append to `index/qa-eval.md`)

Answer and save:
1. Which repos consume the `otxBatchLetter` queue, and who produces to it?
2. For an inbound message hitting `mc-hk-hase-ingress-api`, trace it to the job
   that finally processes it — combining: CodeGraph (sync call into ingress-core)
   + `message_edges.csv` (the event hop) + the consuming `*-tracking-job`.
3. Pick one queue where the producer is config-driven and show how far you can
   trace it and where it goes dark.

## Send back

`index/message_edges.csv` (a sample), `index/message_destinations.md`, and the
qa-eval answers. Note explicitly how much of the producer→consumer mapping was
provable vs config-driven/partial — that tells us how much config modelling the
final assistant needs.

## Producer coverage enrichment

After the read-only dev/SCT export is placed at
`index/tbl_event_router_usecase_topic.snapshot.csv`, run:

```bash
python message_map_enrich.py --dry-run
python message_map_enrich.py
```

The script scans `mirror/` for unique topic/router enum constants, resolves
symbolic destinations in `index/message_edges.csv` to literal topic strings when
the enum evidence is provable, and prints producer coverage plus the remaining
partial ratio. Keep all generated data in `index/`; never write into production
repos.
