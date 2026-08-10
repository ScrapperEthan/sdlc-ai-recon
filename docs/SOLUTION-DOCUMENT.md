# MDC Assistant — Solution Document

| | |
|---|---|
| **Document** | MDC Assistant — Solution Document |
| **Version** | v1.0 (draft) |
| **Date** | 2026-08-10 |
| **Status** | For review |
| **Chinese version** | `docs/SOLUTION-DOCUMENT-zh.md` (section numbering is identical) |

> **How to read this document.** Sections 1–4 cover what this is, why it exists, and where it
> stands; that is sufficient for a management audience.
> **Section 6 is the core of the document** — it explains, hop by hop, exactly which table, which
> file, or which live query every conclusion the assistant produces comes from.
> If you want to answer one question — *"on what basis does it say that?"* — go straight to §6.

---

## 1. Executive summary

MDC is the bank's **notification delivery system** — SMS, email, mobile push, WhatsApp, WeChat and
physical letters to customers all go through it. It is split across **460 Java repositories**, with
one practical consequence: **no single person understands the whole of it.**

MDC Assistant is a question-answering assistant running on an **internal, on-premises model**. You
ask a question in plain language; it reads the code, the configuration, the business routing tables
and — where needed — production logs, and returns an answer in which **every statement carries a
click-through citation**.

**It solves a retrieval problem, not a model problem.** This determines the shape of the entire
solution. Feeding 460 repositories to a language model is neither possible nor advisable. The hard
part is finding *precisely the relevant lines* across 460 repositories and putting them in front of
the model. That layer is what we built. **The model and the UI are replaceable; the retrieval layer
is the durable asset.**

**What works today** (all figures measured against real data):

| Capability | Status |
|---|---|
| Cross-repository Q&A / change impact analysis | 🟢 Live |
| End-to-end business chain (use case → channel → carrier → exit) | 🟢 Live |
| Incident blast radius (alert → who is affected), zero production access | 🟢 Live, 93.2% repository identification rate |
| Root-cause investigation against production (logs / metrics / delivery records) | 🟡 Engine complete and live-verified; blocked on one mapping table |
| New-service scaffolding / templated change to an existing service | 🟡 Thin slice proven (generate → compile → tests green → diff) |
| Automated deployment | ⚪ **Deliberately out of scope** — deployment remains human-gated |

---

## 2. Business context and problem statement

### 2.1 Current state

The MDC notification platform comprises 460 Java repositories, coupled through Maven dependencies
and asynchronous messaging, while the business routing rules live in database configuration tables.
**The code is on one side, the business configuration on the other, and the two do not reconcile.**

### 2.2 Three questions currently answered by "go and find the person who has been here five years"

| # | Question | How it is answered today | Why it is hard |
|---|---|---|---|
| 1 | If I change this shared model, what breaks? | Manual review of POMs; ask a long-tenured colleague | Dependencies are spread across 460 `pom.xml` files; nobody can enumerate them by hand |
| 2 | How does this notification actually reach the customer's handset? | Half from code, half from the database, reconciled mentally | The answer spans the code layer and the business configuration layer; no existing tool sees both |
| 3 | Whose problem is this 3am alert? Do we wake the business up? | On-call engineer's judgement | The alert says a service has high CPU; it cannot say which business use cases are affected |

### 2.3 Conclusion

**The answers to these three questions are distributed across four disconnected layers, and no
existing tool can see all four at once.** That is the rationale for this solution.

---

## 3. Scope

### 3.1 In scope

- Code comprehension, dependency impact analysis and real call-chain tracing across 460 repositories
- Asynchronous messaging topology between services (who publishes, who consumes)
- End-to-end explanation of a business use case: channel → carrier → exit
- Incident business blast radius and root-cause investigation
- New-service scaffolding and templated changes to existing services (output goes to human review)

### 3.2 Explicitly out of scope

These four exclusions are **design decisions, not unfinished work**:

| Exclusion | Rationale |
|---|---|
| **No automated deployment** | Banking environment. Deployment remains a human action; autonomous deployment is not a goal |
| **No writes to production** | Read-only throughout. Not one byte is written to production repositories, databases or logs |
| **No rebuild of the existing AIOps 1.0** | The team already operates a rules-based alert-handling system that works well. We build the layer it cannot serve (*why* it failed, *who* is affected) — not a replacement |
| **No guessing** | If it cannot be retrieved, the assistant says so. **"Not queried" and "queried, nothing found" are always reported as distinct outcomes**; inference is never presented as evidence |

---

## 4. Solution overview

### 4.1 The four-layer model

The assistant's value comes from **connecting four previously disconnected layers**:

| Layer | Contents | Questions it answers | Nature of the data |
|---|---|---|---|
| ① **Code** | Source, dependency graph and cross-repository call chains for 460 repositories | What does changing this class affect? Who calls it? | Index we build |
| ② **Messaging** | Asynchronous wiring between services: who publishes to which topic, who consumes | Where does this message come from, where does it go? | Index we build |
| ③ **Business** | Use cases, channel rules, carrier routing tables | Does this notification go by SMS or email? Which carrier delivers it? | Owner-maintained configuration |
| ④ **Runtime** | Production logs, monitoring metrics, delivery records | Why did it fail at 3am? Was this notification actually sent? | **Queried live against production** |

**Why the layers only have value when connected.** No single layer answers a real question. The code
tells you *"this repository depends on that one"* but not *"which business is therefore affected"*.
The business tables tell you *"this use case uses SMS"* but not *"which repository implements the
SMS service, and what did it log last night"*.

> **The line that stitches these four layers together is the real asset of this project.**

### 4.2 The fundamental distinction between layers ①②③ and layer ④

This distinction matters for compliance and must be stated explicitly:

- **Layers ①②③ are indexes we build in advance** — constructed offline, read-only, never touching a
  running system.
- **Layer ④ is queried live against production at request time.**

Consequently, within any single answer, the parts derived from ①②③ and the parts derived from ④
**carry materially different risk profiles**, and the assistant labels them separately.

---

## 5. How an answer is produced

### 5.1 Mechanism: the model does not memorise the code — it is a tool user

A common misconception is that the 460 repositories are fed to the model. That is neither feasible
nor desirable. The actual mechanism is:

```
Question
   ↓
Assistant decides what to retrieve (selects tools, sets parameters)
   ↓
Retrieval tools query pre-built indexes / live production sources
   ↓
Real results returned
   ↓
Answer with citations
```

**This produces an important property: when it cannot answer, it says so rather than inventing an
answer.** The answer is retrieved, not recalled — and what is not there is reported as not there.

### 5.2 Runtime component flow

```
Browser
  ↓
webapp/server.py          HTTP service (standard library only)
  ↓
webapp/agent.py           Tool loop: decide → call → receive → decide again
  ├→ webapp/llm.py        → internal model (the single model egress point; facade pattern)
  └→ webapp/tools.py      → retriever/          (retrieval layer, 21 tools)
                          → CodeGraph           (real cross-repository call chains)
                          → webapp/mcp_client.py → three MCP servers (runtime layer, live)
                          → webapp/db_readonly.py → read-only UAT database (named queries;
                                                     the model never writes SQL)
```

### 5.3 Every statement carries a citation

Each conclusion is followed by a **clickable citation** that opens the exact source line or
configuration row.

> **You are not required to trust the assistant. You are only required to click through.**

---

## 6. ⭐ Data flow and provenance — where every conclusion comes from

> **This is the most important section of the document.** It answers, hop by hop: *"the assistant
> says use case K3002 goes by SMS, is handed to a given carrier, travels through a given topic and
> leaves through a given exit — on what basis?"*

### 6.1 The flagship chain: use case → topic → delivery → exit → feedback

This is the most frequently asked question, and the one that best demonstrates the value of joining
the four layers:

```
Business use case
   ↓  ①
Channel rules       (SMS or email? which first? what on failure?)
   ↓  ②
Authoritative routing table    (carrier / SLA / delivery tier)
   ↓  ③
Message topic
   ↓  ④
Delivery job
   ↓  ⑤
Outbound API
   ↓  ⑥
Carrier → SMSC / APNs / mail gateway / print exit
   ↓  ⑦
Delivery feedback   (was this one actually sent?)
```

### 6.2 Hop-by-hop provenance — **the core table of this document**

| Hop | What the assistant states | Source of the data | Nature of the source | Confidence |
|---|---|---|---|---|
| ① **Use case attributes** | Business category, owner | Use-case master table (read-only UAT database / exported snapshot) | Owner-maintained configuration | **Authoritative** |
| ② **Channels and ordering** | "SMS first; email only on failure" | Rule expression on the channel-rule table, parsed by us | Owner configuration + **owner-confirmed expression semantics** (2026-07-27) | **Authoritative** |
| ③ **Carrier / SLA / delivery tier** | "Delivered by carrier X, SLA 60000 ms" | Authoritative routing table, joined on its four-column natural key (business category + channel + route + router) | Owner configuration | **Four tiers — see §6.3.** Measured: only **27.3%** reach an authoritative carrier |
| ④ **Message topic** | "Enters the SMS topic" | Committed 7-column architecture node catalogue (with edges) + delivery topology parsed from 460 real repository names | **Architectural fact** | Fact. Parser verified over four live rounds |
| ⑤ **Delivery job repository** | "Consumed by a given delivery-job repository" | As above (repository-name parsing + dependency graph) | **Code fact** | Fact, click-through to the repository |
| ⑥ **Exit** | "Leaves via the carrier's SMSC" | Terminal column of the architecture node catalogue | **Architectural fact** | Fact |
| ⑦ **Was it actually sent** | "The delivery result for this tracking id is X" | **Delivery-records platform (MCP), queried live against production at request time** | **Runtime truth** | Live evidence; **"not queried" and "queried, nothing found" are reported distinctly** |

**Three things to take from this table:**

1. **Hops ④⑤⑥ are facts.** The shape of the pipeline and which carrier serves which channel is
   architecture, not inference. The catalogue is committed to the repository, the topology is parsed
   from real repository names, and the parser has been verified against the live environment across
   four rounds.
2. **Hop ③ is the weakest link in the chain — and it is a limitation of the source data, not of the
   engine** (see §6.3).
3. **Hop ⑦ is the only hop that touches production live.** Its risk profile differs from the other
   six, and it is labelled separately in every answer.

### 6.3 Why carrier conclusions are tiered — and the measured figures

**Conclusions are tiered so that inference is never blended with retrieved fact.** Every time the
assistant names a carrier, it states which tier the statement belongs to:

| Tier | Meaning | How the assistant phrases it |
|---|---|---|
| **Authoritative** | The routing table matched and the carrier display name is owner-confirmed | "It is this carrier" |
| **Authoritative, name unconfirmed** | The authoritative row matched and does name a carrier, but that display name has no owner-confirmed canonical mapping | **Quotes the raw string verbatim**; does not point at any carrier node |
| **Hint** | The channel rule's route/router/sender column contains a known carrier name | "Indicated as this carrier — but whether those columns carry carrier information is unverified" |
| **Upper bound** | No specific carrier is obtainable; every carrier serving the channel is listed | "**At most** these carriers" — **never** "it sends via X" |

**Why this is necessary — measured on the real UAT export:**

| Metric | Measured |
|---|---|
| Rows in the authoritative routing table | 247 |
| Child rows whose four-column key back-links at all | 49.79% |
| Of those, landing on a non-empty carrier | 54.87% |
| **⇒ Child rows reaching an authoritative carrier** | **≈ 27.3%** |
| Distinct non-empty carrier values | **4**, covering push and SMS only |
| Rows with a blank carrier | 58.70% |

**For email, letter, WhatsApp and WeChat the authoritative carrier column is entirely empty** — so
for those channels the upper-bound tier is not a stopgap; **it is the only answer that exists**.

> **Design principle: an honest wide answer beats a confident narrow one.**
> Over-listing carriers costs the reader a sentence. Naming the wrong carrier gets the wrong team
> woken at 3am.

### 6.4 Two conclusions corrected by real data

Neither of these is theoretical. Both were **falsified against real data and then changed**:

**① A carrier at 0% traffic is not "switched off" — it is the standby.**
A configuration row with `traffic = 0%` is a **deliberately configured second carrier**: the one
that takes over when the primary fails.
Measured: **897 channels are "actively sending, with a 0% standby behind them"**.
Before this correction, the question *"if the primary carrier fails, who takes over?"* was
**unanswerable for all 897 of them**.

**② Send ordering carries business semantics.**
In the rule expression, `>` means "send the right side only if the left fails", `&` means "send
together", `|` means "either one".
The consequence: **while the earlier stage is healthy, the later channels are not sending at all** —
so blast radius must not count them all as live traffic.

### 6.5 Incident data flow — two halves with different risk profiles

```
        Paste the raw alert
              │
   ┌──────────┴──────────┐
   │                     │
【WHO IS AFFECTED】  【WHY IT FAILED】
zero production      live production
    access             evidence
   │                     │
repo id 93.2%       ┌────┼────┐
   │                │    │    │
which topics      logs metrics delivery
   │                └────┼────┘
which use cases          │
   │              redacted evidence packet
which channels /         │
  whom to notify         │
   └──────────┬──────────┘
              │
    One complete, cited answer
```

**The two halves are deliberately separate features because their risk profiles differ entirely:
the left half reads not one byte of production; the right half genuinely connects. Inference from
the left is never presented as evidence from the right.**

### 6.6 The three runtime services (**this is not just log search**)

This is the point most easily lost in summary. Three services are integrated, each answering a
different question:

| Service | Contents | Question it answers | Prerequisite for calling it |
|---|---|---|---|
| **Log platform** | Per-application exception and trace logs | "What error did it raise?" | Application name **+ time window (timezone mandatory)** |
| **Metrics platform** | Alarm definitions and metric series | "What shape was the curve before it failed?" | Alarm name + time window |
| **Delivery records** | Delivery outcome for an individual message | "Was this notification actually sent?" | **A tracking id — nothing else** |

⭐ **A fact that must not be lost:** **the largest family of alerts contains neither a repository name
nor an alarm name.** Neither logs nor metrics can serve it — **only the delivery-records path can**.
Treating the runtime layer as "log search" therefore misses precisely the most important path.

All three services have been **live-verified with zero deviation in tool names**.

> ⭐ **The difficulty is not connectivity** — anyone can connect to a log platform.
> **The difficulty is knowing what to query**: which application, which keywords, which time window —
> **all of which are derived from layers ①②③.** That is the value of joining the four layers.

---

## 7. Technology stack

### 7.1 In one line

**Frontend: plain HTML / CSS / JavaScript. No framework, no build step.**
**Backend: Python 3, standard library only. No third-party dependencies at all.**

Nothing in this system needs to be installed — and nothing can be. That is not a convenience
choice; it is what allows the assistant to run inside an air-gapped environment (see §7.5).

### 7.2 Selection overview

| Layer | What is used | Rationale |
|---|---|---|
| **Frontend** | Plain HTML / CSS / JavaScript; no framework, no build step, no npm | Deployment is a directory copy; the browser makes no external request |
| **Backend** | Python 3, **standard library only, zero third-party dependencies** | Runs directly in an air-gapped environment: no pip, no dependency approval, no supply-chain exposure |
| **Model integration** | Internal on-premises model over an OpenAI-compatible HTTP interface | `webapp/llm.py` is the **single** model egress point (facade pattern) with three pluggable providers beneath it → **the model can be swapped with no change elsewhere** |
| **Retrieval layer** | Pre-built index artifacts (dependency graph, message graph, repository tags, delivery topology) persisted as **JSON + CSV files** | Built offline, queried read-only. Build once, query many times; **we operate no database of our own** |
| **Call chains** | CodeGraph (external CLI, indexed as per-domain bundles) | A single graph cannot hold 460 repositories → the estate must be partitioned, paired with narrow-first retrieval. When the CLI is absent from PATH the tool **degrades honestly** rather than fabricating results |
| **Business data** | Exported configuration snapshot + direct read-only UAT database access | The snapshot guarantees offline availability; the direct connection guarantees currency |
| **Runtime layer** | In-house MCP client (two transports, three gates) | Integrates the three colleague-owned MCP services |
| **Database access** | **Named-query** mechanism; **the driver does not live on our side** | ⭐ See §7.4 |
| **Ownership seam** | Eight JSON configuration files under `config/` | ⭐ The intranet team adjusts behaviour by editing configuration — **without waiting for a code push** (see §9) |

### 7.3 Frontend in detail

| Item | Implementation |
|---|---|
| Page | Single-page application in three files: `index.html` (268 lines), `app.js` (2,162 lines), `app.css` (1,457 lines) |
| Framework | **None.** No React / Vue / jQuery, no bundler, no npm, no build step |
| Server communication | The browser's native `fetch()` |
| **Streaming output** | `fetch()` + `ReadableStream.getReader()` reading the response body **incrementally** — the answer renders as it is generated, and tool-call progress appears in the UI in real time |
| Only third-party front-end asset | **`mermaid.min.js`, vendored locally — never from a CDN** (renders the architecture diagrams the assistant emits) |
| Behaviour without it | **Graceful degradation**: if the file is absent, diagram blocks render as their plain source text. No error, no code change required |

> **Key point: the browser issues no external network request.** All static assets are served locally
> by the backend.

### 7.4 Backend in detail

| Item | Implementation |
|---|---|
| HTTP service | Standard-library `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`; routing is hand-written in `do_GET` / `do_POST` |
| Concurrency | Thread per request (standard library) |
| Outbound HTTP | Standard-library `urllib.request` (model calls, MCP calls, retrieval-service proxying) |
| Persistence | **JSON / CSV files.** Sessions, feedback and index artifacts are all files on disk — **we run no database of our own** |
| External processes | `subprocess` invocation of the `codegraph` CLI |
| **Database driver** | ⭐ **There is no database driver in our code.** `webapp/db_readonly.py` imports the **intranet's own runner module** from a path supplied by an environment variable and calls only the two functions specified in their handoff — so a driver such as `psycopg` is **never imported by our core**, which remains standard-library only |
| **The model never writes SQL** | The model may only select a predefined **query name** and pass bound parameters. SQL text, schema, table names and the **column allow-list** all live in configuration; **a PII column that is not on the allow-list can never leave** |

### 7.5 Why zero dependencies — a deliberate decision worth stating

| Benefit | Detail |
|---|---|
| **Runs air-gapped** | No pip, no internal package mirror, no dependency approval process |
| **No supply-chain exposure** | No third-party packages means no third-party vulnerabilities to track, patch or clear through security scanning |
| **Negligible deployment cost** | Deployment is a directory copy plus one Python command. Team access means setting host and port on one internal machine; colleagues open a browser and **install nothing** |
| **Upgrades do not break** | No version conflicts, no failed `npm install`, no stale build artifacts |

### 7.6 Quality assurance

| | |
|---|---|
| Unit tests | **1,364** across 62 test modules |
| Answer-quality evaluation set | **39 cases**, runnable as a regression suite |
| Live verification runbooks | **71 runbooks**, each executed in the real intranet environment with a returned result |

---

## 8. Security and compliance

### 8.1 Posture in one line

**Read-only on production · no data egress · every conclusion independently checkable · generated
output confined to a scratch area · standard library only, air-gap compatible.**

### 8.2 Controls

| Control | Implementation |
|---|---|
| **Read-only** | Read-only throughout against production repositories, databases and logs. Database access runs in a **read-only transaction**, and this is re-verified locally on every call |
| **No egress** | The model runs on the internal network. No data leaves the network |
| **Model does not author SQL** | The model may only select a predefined query name and pass parameters. SQL text, schema, table names and a column **allow-list** live in configuration. **A PII column that is not on the allow-list can never leave** |
| **Unconfigured means refused** | A query left as `?` in configuration is **refused**, and the assistant states that the query is not yet connected. It **never runs a guessed SQL statement, and never reports "not connected" as "no such record"** |
| **Production log redaction** | Three layers of redaction. **Raw logs never enter the model context or the session history** — only a classified, structured evidence packet leaves the process |
| **Generated-output isolation** | Generated code is written only to a scratch directory, **never to a production repository**. Governance values inherited from a reference service (account identifiers, team, internal URLs, organisation codes) are **automatically masked** |
| **No credentials in the repository** | Database addresses, accounts and tokens exist nowhere in this repository; connection is owned entirely by the intranet side |
| **Closed by default** | New data paths are non-callable by default; enabling one requires an **explicit configuration edit** |

---

## 9. Delivery and collaboration model (the intranet/extranet seam)

> This section explains how an air-gapped project is actually built. The process itself is part of
> the project's asset base.

### 9.1 The constraint

**The intranet cannot push code.** This single constraint determines the entire collaboration model.

### 9.2 The loop

```
Extranet: write code / write specification
   ↓  push
Intranet: pull → execute the verification runbook in the real environment
   ↓  returned result
Extranet: fix against the returned result → push again
```

**This loop has now run 71 times.**

### 9.3 Key design: ownership is expressed in configuration, not in code

Because the intranet cannot push, **anything only the intranet can know is routed through a
configuration file**:

| Known only to the intranet | Where it lives |
|---|---|
| Real table names, column names, SQL | `config/db_queries.json` (intranet maintains a local copy) |
| Real MCP tool names, arguments, response shapes, time formats | `config/mcp_tools.json` |
| Business enumerations, channel evidence, alarm patterns, glossary | `config/*.json` |

**Result: the intranet changes one line of configuration to adjust behaviour, with no code push
required.**

Two accompanying disciplines:

- The git-tracked template is **always entirely `?`** and contains no real table names, column names
  or schema.
- The intranet edits a **local copy** (gitignored; the program reads it in preference automatically).
  Otherwise the next `git pull` is **rejected in full** because a tracked file carries uncommitted
  changes — blocking not just that configuration but every fix in that pull, with no way to push back.

### 9.4 The principal benefit of this loop

**Every verification round has caught a real defect, and the defects share a single shape:**

> **Almost every defect was us asserting something about their environment** — asserting a name, a
> response shape, or a value format.
> **The remedy is always the same: we keep the abstract side; they own every real name, shape and
> format.**

---

## 10. Current status and known limitations

### 10.1 Position across the SDLC lifecycle

| Stage | Status | Notes |
|---|---|---|
| Requirements analysis | 🟡 Beachhead | Plain-language request → structured change request → retrieval-grounded target repository and entry point, with citations; refuses to guess when ambiguous |
| **Architecture / impact analysis** | 🟢 **Live** | The core capability, and the deepest |
| **End-to-end business chain** | 🟢 **Live** | The chain now runs to the exit |
| **Incident blast radius** | 🟢 **Live** | Zero production access; 93.2% repository identification on real data |
| **Incident root-cause investigation** | 🟡 Engine complete | All three services live-verified; blocked on data (see §10.2) |
| Code generation | 🟡 Thin slice | Proven end-to-end: generate change → compile → tests pass → emit diff |
| Test / build | 🟢 Thin slice | `mvn test` green on a real service |
| Deployment | ⚪ **Deliberately out of scope** | Banking environment; permanently human-gated |

### 10.2 Three honest limitations

**These figures should not be removed — they are the source of the document's credibility.**

| # | Limitation | Measured | Nature |
|---|---|---|---|
| 1 | **Authoritative carrier coverage is ≈ 27.3%** | 247 routing rows; only 4 distinct carrier values; 58.70% blank; email/letter/WhatsApp/WeChat entirely absent | **A ceiling imposed by the source data, not an engine defect.** The engine already extracts everything obtainable; the remainder requires the owner to supply data |
| 2 | **Root-cause investigation currently has a name mapping for one application** | Measured: mapped repositories = 1, distinct applications = 1 | **A data gap, not a build item.** The engine reads this mapping with no code change required — **this is the highest-leverage unblock in the project**: a working incident engine that can serve only one application |
| 3 | **Hub-repository blast radius is "correct but useless"** | Querying a core repository returns "376 downstream" | Requires blast-radius tiering (genuinely affected vs merely compile-time reachable) |

> **Limitations 1 and 2 should be presented together**: they share one shape —
> **the engine is waiting on data, not the other way round.**

---

## 11. Roadmap and required support

### 11.1 Next steps, ranked by leverage

| Priority | Item | Rationale |
|---|---|---|
| 🔴 **P0** | **Supply the application-name mapping table** | One table extends root-cause investigation from one application to the full estate. **Zero code change** — the highest-leverage item in the project |
| 🟠 P1 | **Confirm carrier display names** | Owner confirmation of the canonical mapping for four carrier display names promotes a tranche of results from "authoritative, name unconfirmed" to "authoritative" |
| 🟡 P2 | **Blast-radius tiering** | Converts "376 downstream" into a usable, tiered conclusion for hub repositories |
| 🟡 P3 | **Widen generated change types** | Currently a templated endpoint addition; extend to message listeners, DAOs and configuration |

### 11.2 Decisions and support required

1. **Endorsement of the direction and continued investment** — in particular the vertical slice that
   moves the assistant from *understanding* to *acting*.
2. **Two data items from the owner side** — the application-name mapping (P0) and carrier display
   name confirmation (P1). Neither requires development work from us, yet each blocks a capability
   that is already built.
3. **A decision on wider rollout** — extending access beyond single-machine use requires deploying
   the assistant as a team service (authentication, audit), which is an environment and compliance
   decision.

---

## Appendix A · Glossary

| Term | Meaning |
|---|---|
| **Use case** | A business notification scenario, e.g. "transfer successful". The start of the chain |
| **Channel** | SMS / email / push / WhatsApp / WeChat / letter / MMS |
| **Channel rule** | Which channels a use case uses and in what order (`>` on failure, `&` together, `\|` either) |
| **Carrier / vendor** | The external provider that actually transmits the message |
| **Exit / terminal** | The final hop: SMSC, APNs/FCM, mail gateway, print exit, etc. |
| **Delivery path** | ⚠️ A **classification**, not a routing control. A message is *classified on* a tier; it does not *travel via* it |
| **Upper bound** | The "at most these carriers" answer given when no specific carrier is obtainable. **Not equivalent to "it is this one"** |
| **MCP** | The protocol by which the assistant calls external tools and data sources. The three runtime services are integrated through it |
| **Runbook** | A verification procedure executed by the intranet team in the real environment, with results returned |

## Appendix B · Related documents

| Document | Purpose |
|---|---|
| `docs/SOLUTION-DOCUMENT-zh.md` | Chinese version of this document |
| `docs/MDC-ASSISTANT-INTRO-zh.md` | Full capability-by-capability introduction (579 lines, for a first-time reader) |
| `PROJECT-STATE.md` | Stage-by-stage status (living document) |
| `docs/TOOL-LIST-AND-MAINTENANCE-zh.md` | Retrieval tool inventory and maintenance notes |
| `RUNBOOK-*.md` | 71 live verification runbooks |
