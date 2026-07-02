# HASE AI Engineering Assistant — one-pager

**What it is.** An internal AI assistant for the HASE estate (~390 Maven/Spring repos,
org `hase-mc`), running on the internal GPT-5.5. It helps engineers **understand** the
whole system and **create** new services that follow our conventions — grounded in our
own code, read-only on production, with no data leaving the network.

**Why it matters.** The estate is one product split across ~390 near-identical repos.
The bottleneck isn't the model — it's **retrieval**: knowing, across 390 repos, what a
change touches and how a new service should look. We've built a durable layer for exactly
that (the "moat"); the model and UI are swappable, the retrieval layer is the asset.

## Working today

- **Understand (live).** Ask in plain English — *"if I change `EventPayload`, what
  breaks?"*, *"how does ingress route to a topic?"* — and get an answer with **cross-repo
  impact + `repo/path:line` citations**. Runs end-to-end on a 15-repo pilot flow.
- **Create (pilot, just completed).** One command → a new service skeleton that already
  follows the shared parent/starter conventions (Java 21, package convention, platform
  config, source layout). Every value inherited from the reference service (account IDs,
  team, Sonar branch policy, internal URLs, org codes) is **auto-blanked to `<REVIEW>`**
  so nothing wrong is silently carried over. Output goes to a **scratch folder — never a
  production repo**.

## 3-minute live demo

1. **Understand** — ask an impact question → cross-repo answer with citations.
   *"It reads our code, not generic knowledge."*
2. **Create** — `python -m scaffold.generate payments` → a convention-perfect new service
   in seconds. *"New engineers can't miss our standards."*
3. **Safe** — show it only wrote to `scratch/`, cites every source, and sanitized the
   reference service's account/team/URL values. *"Governable, auditable, can't touch prod."*

## Safe by design (bank-ready posture)

Read-only on production · runs on internal GPT-5.5, **no data egress** · every claim
**cited and checkable** · generated output is **scratch-only** with governance values
**auto-sanitized** · standard-library, air-gap-friendly.

## Where we are, and what's next

Across the SDLC lifecycle: **architecture/impact understanding 🟢 live**, **code-generation
🟡 beachhead (scaffolding)**, test / build / deploy ⚪ not yet (deploy stays **human-gated**
by design). Full status in `PROJECT-STATE.md`.

**Next build — a thin "vertical slice":** generate a *real* code change to an existing
service, **compile it and run its tests to green**, and hand a diff for human review.
That turns a skeleton into *a change that provably works*, and pulls testing + build into
the loop — the most credible next capability to demonstrate.
