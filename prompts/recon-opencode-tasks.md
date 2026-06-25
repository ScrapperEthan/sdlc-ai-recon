# Recon tasks for opencode (or any agentic coder)

These are the **qualitative** questions a static script can't answer well. Run
them with opencode pointed at the repos (a local mirror, or clone-on-demand).
The **structural** questions (is it Maven multi-repo + shared libs, what's the
dependency graph) are answered deterministically by `recon_maven_graph.py` — run
that first; see the repo `README.md`.

## Context to give opencode

> We have ~400 GitHub repos that together form **one** system (not 400 separate
> projects). Mostly Java, strong naming convention `mc-hk-hase-*`, with layers
> split across repos (`*-api-starter`, `*-api-domain`, `*-api-dao`), plus batch
> `*-job` repos, a JS frontend, and Terraform/Python infra. I'm building an
> AI coding assistant and need to understand the architecture. Read code and
> cite every claim as `repo/path:line`. Don't guess — if unsure, say so.

---

## Task 1 — Runtime coupling (how services actually talk)

```
Scan the repos and determine how services communicate AT RUNTIME (not just
compile-time Maven deps). Look for:
  - REST clients: OpenFeign (@FeignClient), RestTemplate, WebClient, generated
    OpenAPI/Swagger clients
  - Messaging: Kafka, RabbitMQ, AWS SNS/SQS/EventBridge — I saw an
    `AWS-TF-CommonBus` / EventBus repo, so event-driven is likely
  - gRPC, or direct DB sharing

Output as markdown:
  1. A table: mechanism | approx. # of repos using it | example (repo/path:line)
  2. The top ~15 Feign clients or message topics/queues by frequency, each with
     producer repo and consumer repo(s) if determinable
  3. One sentence: is this system primarily synchronous (REST) or event-driven?
```

## Task 2 — Platform base (the golden template's source of truth)

```
Find the shared foundation every service is built on. Specifically:
  - Is there a common PARENT pom (e.g. a `*-parent` or `*-bom`) that most repos
    inherit? Which repo defines it?
  - Is there a base "starter" library (the `*-api-starter` pattern) that wires
    cross-cutting concerns? List exactly what it standardizes: logging, config,
    exception handling, pagination, auth, tracing, etc.
  - What Spring Boot version and Java version are used? Note any version spread.

Output as markdown:
  1. The parent/BOM repo + artifact coordinates, and how many repos inherit it
  2. A bullet list of conventions the starter enforces, each cited repo/path:line
  3. Spring Boot + Java versions (and any repos that diverge)
This is the spec we'll turn into an AI scaffolding generator (roadmap P3).
```

## Task 3 — Representative vertical slice (for the repo guide)

```
Pick ONE typical business service (NOT a library) and explain it end to end so a
new engineer — or an AI assistant — could work in it. Cover:
  - The layer split: how `api-starter` / `api-domain` / `api-dao` relate for this
    service, and which repos hold each
  - Which internal shared libraries it depends on (from its pom)
  - What it exposes (REST endpoints / events published) and what it consumes
  - Where the real business logic lives vs boilerplate
  - How to build and run it locally

Output as a `REPO-GUIDE.md` draft for that service: conventions, entry points,
"where do I add X", gotchas. Cite repo/path:line throughout.
```

---

## After these run

Hand back to me: `recon_out/summary.txt`, `recon_out/top_shared.csv`, plus the
three markdown outputs above. With those I can pin down the P0 pilot scope, the
first retrieval-layer component to build, and the repo-guide template.
