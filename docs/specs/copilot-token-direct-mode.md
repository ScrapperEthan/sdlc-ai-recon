# Spec: Paste-token "direct Copilot" mode — INTERNAL BETA (temporary)

Status: DRAFT. **Scope deliberately minimal.** This is a throwaway feature for the
internal test phase only: let testers paste their `.copilot_token` and chat on their own
Copilot quota, no tunnel required. It ships behind one flag (default OFF) and is
**removed before GA** (see §9). Companion to the internal Chinese note
`docs/COPILOT-TOKEN-DUAL-MODE-REFACTOR-PLAN-zh.md`.

The earlier "production ladder" (HTTPS hard-gate, SSO, credential vault) is **out of
scope** — the team accepted the residual risk for a temporary internal beta.

## 1. Goal

Today users reach the LLM only via **tunnel mode**: run `copilot-api` locally + reverse
tunnel, register the loopback endpoint (RUNBOOK-41). Add an optional **token mode**: a
tester pastes `.copilot_token` in the web page, the server does the GitHub token exchange
and calls Copilot directly, so each tester spends **their own** quota with nothing running
locally. Tunnel mode is unchanged and stays the default.

## 2. Scope / non-goals (internal beta)

- One env flag `SDLC_LLM_TOKEN_MODE`, default **OFF**. Feature only exists when it's on.
- Enabled only on the internal test deployment. **MUST NOT** be turned on for any
  external or production entrypoint. Removed before GA.
- **No** HTTPS hard-gate, **no** SSO, **no** persisted credential vault, **no** PoC
  allowlist. Those were the complexity we're cutting.
- Residual risk, accepted: on the plain-HTTP box the pasted token crosses the internal
  network in cleartext. Acceptable for a temporary internal beta; not for prod.

Two things we keep because they are *simpler*, not more complex, and stop the beta from
becoming a footgun:
- The credential lives in **process memory only** — never `llm_routes.json`, never any
  file. (In-memory is less code than persisting it.)
- The token, the derived service token, and `Authorization` headers are **never logged**.

## 3. Current architecture (the seam we build on)

The per-request override machinery from tunnel mode is the extension point.

- `webapp/llm.py:15` `_provider_module()` selects a provider from the **global**
  `config.LLM_PROVIDER`. Facade stays tiny (merge-conflict rule).
- `webapp/config.py:24-51` — `LLM_*` endpoint fields resolve per-request via a
  `contextvars` override; `set_llm_override(dict)` / `reset_llm_override(token)` bind for
  one request thread. **Today the override carries `base_url`/`api_key`/`model` — NOT the
  provider.**
- `webapp/llm_routes.py` — disk-backed tunnel route registry; `_validate_base_url`
  (`:47`) enforces a loopback host (SSRF boundary).
- `webapp/server.py` — `_user_token()` (`:62`) reads `X-SDLC-User-Token`; the chat handler
  resolves it to an override and wraps the turn in `set_llm_override` (`:236-250`).
- `webapp/llm_providers/copilot_responses.py:134` — reads `config.LLM_*`, POSTs to
  `<base_url>/responses`. Marked "INTERNAL SIDE OWNS THIS FILE" — network-facing provider
  code is verified on the box.

## 4. Design — small, self-contained, deletable

### 4a. `provider` becomes per-request (`config.py` + `llm.py`)
Token users and tunnel users need different providers in one server. Add `provider`
(and a `mode` marker) to the override context, resolved like the endpoint fields
(override wins, else `LLM_PROVIDER` env default). `llm.py:15` `_provider_module()` reads
the override-aware provider and registers `github_copilot_direct`. When no override sets
`provider`, behaviour is byte-for-byte identical to today.

### 4b. New provider `webapp/llm_providers/github_copilot_direct.py`
Two-stage token flow (OAuth/Copilot credential → short-lived service token → call Copilot
`chat/completions` → convert to the app's chat-style message, reusing the same
conversion shape as `copilot_responses`). **All endpoints from env**, no proxy password /
cert / internal host baked in, explicit connect+read timeouts, distinct 401/403/429/
proxy/cert errors, no secret logging. The actual token-exchange URLs/shapes + enterprise
proxy/CA are verified on the box → **internal Codex** (§8).

### 4c. `webapp/llm_credentials.py` — RAM-only store
```
credential_id -> owner_uid, oauth token (RAM), cached service token + expiry (RAM)
              -> encrypted-at-rest: N/A, never written
```
Connect: browser sends the pasted token once, server keeps it in RAM, returns an opaque
`credential_id`. Each chat sends only `credential_id`. Disconnect drops it.

### 4d. Endpoint pinning (keep SSRF surface closed)
Token mode talks to a **fixed Copilot endpoint pinned from env**; the request carries a
`credential_id` and **no base_url**. Do NOT route token mode through the user-supplied
`base_url` path in `llm_routes.py`. Keep the two paths disjoint.

## 5. Per-request context (extends today's override)
```jsonc
{ "mode": "tunnel" | "copilot_token",
  "provider": "copilot_responses" | "github_copilot_direct",
  "base_url": "...", "model": "...",   // tunnel only (loopback-validated)
  "credential_id": "..." }             // token only — opaque ref, NOT the token
```

## 6. HTTP endpoints (`server.py`, all behind the flag)
- `POST /api/llm/connect-token` — body: pasted `.copilot_token`. Returns `{ credential_id }`.
  404/disabled when the flag is off.
- `POST /api/llm/disconnect-token` — drops the credential from RAM.
- `GET /api/llm/me` (`:140`) — extend to report the mode (never echo any token).

## 7. UI (existing "my LLM" panel, `webapp/static/index.html`)
Add a two-choice selector:
```
( ) Reverse tunnel   Endpoint: http://127.0.0.1:4142/v1
( ) Copilot token — no tunnel   Paste .copilot_token: ********
[Connect] [Disconnect]
```
Token option only shown when the flag is on.

## 8. Work split

**Local — buildable + testable here without a real Copilot endpoint (Sonnet 5 / me):**
- 4a provider-per-request (`config.py`, `llm.py`) + tests.
- 4c `llm_credentials.py` RAM store + tests.
- 4b provider **scaffold**: env reads, timeouts, message conversion, error taxonomy, with
  `# CODEX-VERIFY` markers where real network specifics go.
- 6 server endpoints + context wiring, all flag-gated.
- 7 UI.
- Extend `tests/test_llm_routing.py`: flag-off = identical; provider isolation across two
  concurrent contexts; credential store is RAM-only and never logged; connect/disconnect.

**Internal Codex — only the box can finish/verify:**
- The real GitHub token-exchange request/response shapes in 4b (URLs, headers, field
  names) against the actual Copilot endpoint.
- Enterprise proxy + CA config for server egress to Copilot.
- Confirm the converted response matches what `agent.py` expects end-to-end on real traffic.

## 9. Removal plan (before GA)
Because it's one flag + two new files + small hunks:
1. Set `SDLC_LLM_TOKEN_MODE` off (kills it immediately), then
2. delete `webapp/llm_providers/github_copilot_direct.py` and `webapp/llm_credentials.py`,
3. revert the small additions in `config.py`, `llm.py`, `server.py`, `index.html`.
No schema/disk migration to undo (nothing was persisted).

## 10. Acceptance criteria
1. Flag off (default): behaviour identical to today; all existing routing tests pass.
2. Flag on: paste a token → chat routes through `github_copilot_direct` (scaffold path
   testable with a stubbed exchange); credential lands only in RAM.
3. Two concurrent users (one tunnel, one token) each get the right provider — no leakage.
4. After a token chat, logs contain **zero** occurrences of the token / service token /
   `Authorization: Bearer`; no on-disk file contains the credential.
5. Disconnect removes the credential; a later chat with the stale `credential_id` fails closed.
