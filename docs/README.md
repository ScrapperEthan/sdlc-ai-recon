# Docs map — who reads what

Two agents plus you. Give each only what it needs.

## 🌐 External Codex — IMPLEMENTS from specs (has internet, no intranet access)

Give it:
- **`docs/specs/*.md`** — one build-ready spec per feature; each says exactly what
  to change and how. This is its whole job.
- **`BACKLOG.md`** — for project context + the **Guardrails** (read the guardrails
  every time: read-only prod, air-gapped/stdlib, bank security, the `llm.py`
  facade merge rule, the message-shape contract).

It writes code in a branch and stops at each spec's **"Done when"**. It does NOT
run anything against the intranet.

## 🏢 Internal Codex — RUNS + VERIFIES (on the intranet: real repos, copilot-api)

Give it:
- **`RUNBOOK.md`, `RUNBOOK-2-cross-repo-qa.md`, `RUNBOOK-3-message-map.md`** — build
  the recon data + indexes on the intranet.
- **`RETRIEVER.md`, `WEBAPP.md`** — how to run the tools and the web app.
- The **"Acceptance criteria" + "Verification steps"** section at the bottom of each
  `docs/specs/*.md` — run these AFTER external Codex's code is pulled.
- It owns **`webapp/llm_providers/copilot_responses.py`** (verify/adjust against the
  live copilot-api).

## 👤 You (maintainer)

- **`README.md`** — overview.
- **`BACKLOG.md`** — the menu, priorities, and items marked **needs a decision**
  (⑤ scale-out, ⑨ deployment) — decide those before either Codex starts.

## The loop

```
you pick a spec  →  external Codex implements  →  you push
   →  internal Codex verifies (Acceptance + Verification)  →  merge
```

## Spec index (`docs/specs/`)

| Spec | What it adds |
|---|---|
| `streaming.md` | Real streaming: live tool steps + answer types out |
| `citation-verification.md` | Prove every cited `repo/path:line` actually exists |
| `clickable-citations.md` | Click a citation → view the real source, line highlighted |
| `eval-harness.md` | Repeatable, scored answer-quality regression |

Not a spec (config/reference, both sides may read): `prompts/qa-system-prompt.md`
(the assistant's behaviour rules).
