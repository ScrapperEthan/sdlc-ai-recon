# RUNBOOK 5 — evaluate the peer team's AI-SDLC skill-hub (read-only)

Goal: confirm the internal box can actually **access** the peer team's "AI-SDLC workshops
skill-hub" repo (a set of GitHub-Copilot Agents + Skills another HASE team built for a
requirements → design → code → review → test workflow), then **inventory and assess** it so we
can decide two things:

1. **Reuse** — which of their skills we can lift to fill *our* pipeline front-end (the stages we
   have NOT built: requirements → PRD, design → tech-design, and SIT test cases).
2. **Integration** — where our cross-repo retrieval **moat** (`retriever/` + `mcp_server.py`)
   would plug in as a *new* skill (e.g. `cross-repo-impact`) so their Plan/Coder/Review agents
   gain the cross-repo grounding they currently lack.

This is an **internal Codex** job — it only **reads** another team's repo. Local background (if
present on the box): `docs/PEER-TEAM-ANALYSIS-zh.md`.

> **Read-only & safe — hard rules:**
> - Clone the peer repo to a **throwaway dir OUTSIDE our repo**. Never clone it inside this
>   working tree (so it can't get committed by accident).
> - Do **not** modify or push the peer repo. Do **not** run any skill/agent — only read files.
> - Do **not** touch `mirror/` or anything under `hase-mc`.
> - Do **not** commit anything (you're air-gapped and can't push anyway — the user collects the
>   report).
> - Do **not** put secrets, tokens, internal hostnames, or internal URLs into the report.
>   Report only *that* such a value exists and its variable/field **name** — never the value.

## Fill these in on the box

```
SKILL_HUB_URL = <clone URL of the peer skill-hub — the "Agents and Skills files git repo" link
                 on the Confluence "implementation guideline" page>
EVAL_DIR      = <a scratch dir OUTSIDE this repo, e.g. C:\D_Data\peer-skill-hub-eval>
```

Use whatever git auth you already use to reach the `mirror/` source repos on the internal host.

---

## Step 0 — Access check (this is itself a key finding)

```
git clone "%SKILL_HUB_URL%" "%EVAL_DIR%"
git -C "%EVAL_DIR%" log -1 --format="last commit: %H %cd" --date=short
```

If the clone **fails** (403 / auth / not-found), **STOP and report exactly that** — "can Codex
reach these skills at all?" is the first question we need answered. If it succeeds, note the
default branch and the last-commit date (are they still actively updating it?).

## Step 1 — Inventory the structure

Use git plumbing so it's shell-independent:

```
git -C "%EVAL_DIR%" ls-files
```

Report:
- The directory layout, and **where agents vs skills live** (the guideline mentioned locations
  like `.github/agents/`, `.github/skills/`, `.copilot/…`, `.claude/agents/` — report what's
  actually there).
- The **exact list of agents and skills present**, compared to what we expected to see:
  - **Agents:** Plan, Coder, Review.
  - **Skills:** `jira-data-search`, `rag-data-search`, `prd-writing`, `tdd-writing`,
    `domain-papi-plan`, `domain-papi-dev`, `domain-papi-review`, `api-sit-case-design`,
    `api-sit-case-reviewer`, `pib-api-test-executor`, `api-test-result-reviewer`.
  - Note anything **extra or missing**.
- The **file format** of a skill/agent: a single `.md`? a `.prompt.md`? a folder with a
  manifest (`.yaml`/`.json`)? Something Copilot-specific?

## Step 2 — How is a skill/agent defined? (portability + our integration hook)

Open **1 agent** (e.g. Plan) and **~4 representative skills** (`prd-writing`, `tdd-writing`,
one `domain-papi-*`, one `api-sit-case-*`). For each, capture:

- **Pure prompt vs runtime-bound:** is it just portable markdown/instructions (model-agnostic),
  or does it bind to **Copilot-specific runtime / tools**?
- **Tool/data declaration mechanism (IMPORTANT — this is our attach point):** does the format
  provide a way to declare **tools / commands / data sources / MCP servers**? If yes, report the
  **verbatim field names / syntax** (e.g. a `tools:` block, an `mcp` reference, a shell command
  the skill runs). This tells us how a new `cross-repo-impact` skill would attach.
- **Inputs/outputs:** which `.md` files it consumes and produces.
- **External endpoints / secrets:** does it hardcode a RAG URL, a Jira host, or a token name?
  Report **only the field/variable name and that it exists** — not the value.
- Rough size / complexity.

## Step 3 — Reuse verdict (fill-ins for our missing front-end)

Judge each skill that maps to a stage **we have not built**:

| Our missing stage | Their skill(s) | Verdict |
|---|---|---|
| Requirements → PRD | `jira-data-search`, `prd-writing` | portable as-is / needs edits / Copilot-locked |
| Design → tech-design/spec | `tdd-writing`, `domain-papi-plan` | … |
| SIT test cases + run | `api-sit-case-design`, `api-sit-case-reviewer`, `pib-api-test-executor`, `api-test-result-reviewer` | … |

For each, note the hard **dependencies** (needs their RAG? needs Jira? needs a specific runtime?)
that would block a straight lift.

## Step 4 — Integration hook for our moat

Based on Step 2's tool mechanism:
- **Do their skills already call an MCP server?** If yes → ours plugs in the same way (we ship
  `mcp_server.py`); report how they register one.
- If skills are prompt + a RAG lookup only → note what a new `cross-repo-impact` skill would have
  to shell out to (our `cli.py` / `retriever/`).
Report the **concrete attach point** in one or two sentences.

## Step 5 — Data-boundary & safety scan (feeds the bigger "could we adopt Copilot?" question)

Skim for facts only (do **not** decide anything, do **not** copy any secret):
- Any egress to a **non-intranet** endpoint?
- Any **committed secrets/tokens** (report Y/N and the file + variable name, never the value)?
- How is the RAG endpoint referenced (hardcoded vs env var)?

## Write the report

Write the full report to a **local** file **outside** our repo
(e.g. `%EVAL_DIR%\..\PEER-SKILL-HUB-EVAL-REPORT.md`) **and echo it to stdout**. Do **not** commit
it. The user photographs / relays it back. Redact internal URLs / hostnames / tokens as above.

## Send back (paste this filled in)

```
Step 0 access:        [ clone OK? or exact error. default branch, last commit date ]
Step 1 inventory:     [ #agents, #skills; the list; file format; extra/missing vs expected ]
Step 2 skill format:  [ pure-prompt vs Copilot-locked; tool/MCP declaration mechanism
                        (verbatim field names/syntax); endpoint/token NAMES present (no values) ]
Step 3 reuse verdict: [ per stage: portable / needs-edits / locked + blocking deps ]
Step 4 moat hook:     [ do they use MCP? the concrete attach point for cross-repo-impact ]
Step 5 safety:        [ non-intranet egress? committed secrets (Y/N + file/var, no value)? ]
Surprises / errors:   [ ... ]
```

**What each outcome means:**
- **Step 0 fails** → that's the headline; we either get access sorted with the peer team or
  rethink the reuse plan. Nothing else matters until access works.
- **Steps 1–4 look good** → next, on the external/pushable side we (a) draft a `cross-repo-impact`
  skill against their exact format and (b) pick the front-end skills to adopt into our pipeline
  (aligning our Phase-4 `change/from_intent` artifacts to their `PRD.md` / `Technical Design.md`
  / `Plan.md` naming). See `docs/PEER-TEAM-ANALYSIS-zh.md` §6.
