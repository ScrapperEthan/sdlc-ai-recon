"""Web app config — all via env vars so Codex/ops can set them without code edits.

Multi-user LLM routing: the five ``LLM_*`` endpoint fields below are NOT plain module constants —
they are resolved through ``__getattr__`` against a per-request ``contextvars`` override. The env
values are the DEFAULT (single-user / unset case, unchanged); when the server binds a request to a
user's own LLM (their reverse-tunnel loopback port), every ``config.LLM_BASE_URL`` read in that
request's thread returns that user's endpoint instead. This keeps the provider files
(``llm_providers/*``, internal-owned) completely untouched — they still just read ``config.LLM_*``.
"""
import contextvars
import os

# ---- model: provider defaults to global (all users run the same local copilot-api) but is now
#      resolvable per-request too (see _LLM_DEFAULTS/__getattr__ below) -- the internal-beta
#      paste-token mode (SDLC_LLM_TOKEN_MODE) needs a token-mode caller to get a DIFFERENT provider
#      (github_copilot_direct) than everyone else in the same process, same mechanism as the
#      per-user endpoint override. The ENDPOINT is also per-user (see below). ----
LLM_MOCK = os.environ.get("LLM_MOCK", "") not in ("", "0", "false", "False")
# Opt-in true token streaming (Responses API SSE). OFF by default so behaviour is unchanged until
# the internal side turns it on and verifies against its copilot-api; any streaming failure falls
# back to the blocking call automatically. See webapp/llm_providers/copilot_responses.chat_stream.
LLM_STREAM = os.environ.get("LLM_STREAM", "") not in ("", "0", "false", "False")

# The per-user-overridable endpoint fields. Env value = default; override key = the field name
# without the LLM_ prefix, lower-cased (LLM_BASE_URL -> base_url).
_LLM_DEFAULTS = {
    "LLM_BASE_URL": os.environ.get("LLM_BASE_URL", "http://127.0.0.1:4141/v1"),
    "LLM_API_KEY": os.environ.get("LLM_API_KEY", "dummy"),
    "LLM_MODEL": os.environ.get("LLM_MODEL", "gpt-5.5"),
    "LLM_MAX_TOKENS": int(os.environ.get("LLM_MAX_TOKENS", "4096")),
    "LLM_TIMEOUT": int(os.environ.get("LLM_TIMEOUT", "120")),
    # Provider is now resolved the same way as the endpoint fields above (override wins, else this
    # env default) so a token-mode request can select a different provider than everyone else in
    # the same process. When no override sets "provider" (i.e. always, until token mode exists),
    # this is byte-for-byte the same value the old plain `LLM_PROVIDER = os.environ.get(...)`
    # module constant used to hold.
    "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", "copilot_responses"),  # or "openai_chat"
    # Opaque reference into the RAM-only credential store (webapp/llm_credentials.py). Token-mode
    # only; empty for everyone else. Never the token itself -- see SDLC_LLM_TOKEN_MODE below.
    "LLM_CREDENTIAL_ID": "",
    # Who this request is FROM (server.py's self._uid), threaded alongside LLM_CREDENTIAL_ID so the
    # provider (webapp/llm_providers/github_copilot_direct.py, internal-owned) can re-verify
    # ownership itself -- llm_credentials.resolve/update_service_token take a matching owner_uid= --
    # rather than trusting that server.py's own check was sufficient. Token-mode only; empty
    # otherwise.
    "LLM_CREDENTIAL_OWNER_UID": "",
    # The model the user actually confirmed (via connect-probe or POST /api/llm/select-model),
    # resolved the same override-then-env way as everything else. Distinct from LLM_MODEL: LLM_MODEL
    # is what providers put in their request payload (so provider code needs zero changes to pick up
    # a dynamic selection -- the override sets both keys to the same value), while
    # LLM_SELECTED_MODEL is what server.py's status endpoints (/api/llm/me, connect/select-model
    # responses) read to report the confirmed model without depending on any provider internals.
    "LLM_SELECTED_MODEL": "",
}
_llm_override = contextvars.ContextVar("sdlc_llm_override", default=None)

# ---- internal beta: paste-token "direct Copilot" mode (THROWAWAY -- removed before GA) ----
# See docs/specs/copilot-token-direct-mode.md. One flag, default OFF. When off, no new code path is
# reachable and behaviour is identical to before this feature existed (existing routing tests pass
# unchanged). MUST NOT be turned on for any external/production entrypoint -- internal test
# deployment only.
LLM_TOKEN_MODE_ENABLED = os.environ.get("SDLC_LLM_TOKEN_MODE", "") not in ("", "0", "false", "False")


def __getattr__(name):
    """Resolve the overridable LLM_* fields per-request (contextvars) with env fallback (PEP 562).

    Only called for names not defined as real module attributes, so the static config above is
    unaffected. Each request thread has its own context, so a set override never leaks across users.
    """
    if name in _LLM_DEFAULTS:
        override = _llm_override.get()
        if override:
            key = name[len("LLM_"):].lower()
            value = override.get(key)
            if value not in (None, ""):
                return value
        return _LLM_DEFAULTS[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def set_llm_override(override):
    """Bind this request's LLM endpoint. `override` is a dict (base_url/api_key/model/…) or None to
    use the env default. Returns a reset token to pass to `reset_llm_override` in a finally block."""
    return _llm_override.set(override or None)


def reset_llm_override(token):
    _llm_override.reset(token)


def llm_default_base_url():
    """The env-default endpoint, ignoring any active override (for status/health display)."""
    return _LLM_DEFAULTS["LLM_BASE_URL"]


def llm_default_provider():
    """The env-default provider, ignoring any active override (for status/health display + tests)."""
    return _LLM_DEFAULTS["LLM_PROVIDER"]


def llm_default_model():
    """The deployment's default model, ignoring any active override (for status display, and for
    picking a starting model to probe before anything has been selected yet)."""
    return _LLM_DEFAULTS["LLM_MODEL"]


# ---- assistant behaviour ----
SYSTEM_PROMPT = os.environ.get(
    "SDLC_SYSTEM_PROMPT", os.path.join(os.getcwd(), "prompts", "qa-system-prompt.md")
)
MAX_TOOL_ITERS = int(os.environ.get("SDLC_MAX_TOOL_ITERS", "8"))
TOOL_RESULT_CAP = int(os.environ.get("SDLC_TOOL_RESULT_CAP", "12000"))  # legacy char cap, unused

# ---- context budget (webapp/context_budget.py) ----
# ONE budget per turn, divided into lanes. Independent per-thing caps cannot prevent running out of
# context: their worst cases just add up and nothing watches the total. Everything below is
# estimated (no tokenizer offline) and deliberately pessimistic.
#
# Sized for the deployment's model. 128k is a safe floor for the Copilot models in use; raise it
# when the model is known to be larger, lower it if requests start getting rejected for length.
CONTEXT_TOKENS = int(os.environ.get("SDLC_CONTEXT_TOKENS", "128000"))
# Kept empty so the model has room to answer. Matches the output cap it is given.
OUTPUT_RESERVE_TOKENS = int(os.environ.get("SDLC_OUTPUT_RESERVE", os.environ.get("LLM_MAX_TOKENS", "4096")))
# Estimates run high on purpose: over-estimating costs a slightly shorter answer, under-estimating
# costs a failed request.
TOKEN_SAFETY_FACTOR = float(os.environ.get("SDLC_TOKEN_SAFETY", "1.15"))
# How the working budget (total - system prompt - output reserve) is divided. `compaction` is
# RESERVED, not yet filled: summarizing dropped turns needs an extra model call, so it is deferred,
# but the lane exists now so adding it later fills a hole instead of re-plumbing the budget.
# `subagent` is for the phase-2 incident investigator's evidence packet.
CONTEXT_LANE_SHARES = {
    "history": float(os.environ.get("SDLC_LANE_HISTORY", "0.25")),
    "compaction": float(os.environ.get("SDLC_LANE_COMPACTION", "0.05")),
    "tools": float(os.environ.get("SDLC_LANE_TOOLS", "0.50")),
    "subagent": float(os.environ.get("SDLC_LANE_SUBAGENT", "0.20")),
}
# History is bounded twice: by tokens (protects the request) and by rounds (protects answer
# quality -- forty turns of context makes the model worse at the question actually being asked,
# even when it all fits). 0 disables the round cap.
HISTORY_MAX_ROUNDS = int(os.environ.get("SDLC_HISTORY_MAX_ROUNDS", "10"))
# Per-STRING cap inside a tool result, in characters. Bounds the one shape a byte truncation
# mangled worst -- a single enormous string, i.e. exactly what a log excerpt is -- while leaving
# the surrounding JSON intact and marking what was dropped.
TOOL_STRING_CAP = int(os.environ.get("SDLC_TOOL_STRING_CAP", "4000"))
SESSION_STORE = os.environ.get(
    "SDLC_SESSION_STORE", os.path.join(os.getcwd(), "webapp_data", "chat_sessions.json")
)
# Ask the model for a short session title on the FIRST exchange of a session (webapp/session_title.py).
# One extra short model call per session, not per turn. Off => the sidebar keeps the truncated
# question, which is what it showed before this existed.
SESSION_TITLE_LLM = os.environ.get("SDLC_SESSION_TITLE_LLM", "1") not in ("", "0", "false", "False")
# Per-user LLM route registry (token -> their loopback endpoint). Gitignored like the session store.
LLM_ROUTES_STORE = os.environ.get(
    "SDLC_LLM_ROUTES", os.path.join(os.getcwd(), "webapp_data", "llm_routes.json")
)
# Safety: a registered endpoint must be loopback (each user's LLM is reached via THEIR server-side
# reverse-tunnel port, always 127.0.0.1:<port>). This also blocks SSRF to arbitrary internal hosts.
# Set to "1" only if a deployment deliberately uses non-loopback connector hosts.
LLM_ALLOW_NONLOOPBACK = os.environ.get("SDLC_LLM_ALLOW_NONLOOPBACK", "") not in ("", "0", "false", "False")

# ---- MCP (the colleagues' LogDream / CloudWatch / Portal servers) ----
# Default OFF, same discipline as SDLC_LLM_TOKEN_MODE: with this unset no code path in
# webapp/mcp_client.py can open a socket, so behaviour is identical to before it existed. Turning it
# on is not sufficient by itself either — the server must also be `enabled` in the intranet's
# mcp_tools.json AND have its url_env set, so there are three independent gates before any
# production system is contacted.
MCP_ENABLED = os.environ.get("SDLC_MCP_ENABLED", "") not in ("", "0", "false", "False")
# Per-call wall clock. RUNBOOK-55 measured `list_alarms` at 26.4s for 500 rows, so anything under
# ~30s would time out on legitimately slow calls and look like an outage.
MCP_TIMEOUT = int(os.environ.get("SDLC_MCP_TIMEOUT", "60"))
# Hard byte cap on one response. A log read can return far more than the context budget can hold;
# truncating at the socket keeps a runaway response from becoming a memory problem before
# context_budget ever sees it. Truncation is always reported, never silent.
MCP_MAX_RESPONSE_BYTES = int(os.environ.get("SDLC_MCP_MAX_BYTES", "4000000"))
# The MCP servers are internal infrastructure reached by hostname; routing them through the company
# HTTP proxy is never correct and is exactly what RUNBOOK-60 hit — every MCP call came back 403 from
# the proxy until the host was added to NO_PROXY. So by default this client bypasses system proxies
# for MCP requests, which means a deployment does NOT have to remember to persist NO_PROXY. Set this
# to "1" only for a deployment that genuinely reaches its MCP endpoint through a proxy.
MCP_USE_PROXY = os.environ.get("SDLC_MCP_USE_PROXY", "") not in ("", "0", "false", "False")
# Advertised protocol version per transport. Env-overridable because a version bump on their side
# must not require an external code change and a push we may not be able to deliver.
MCP_PROTOCOL_STREAMABLE = os.environ.get("SDLC_MCP_PROTOCOL_HTTP", "2025-03-26")
MCP_PROTOCOL_SSE = os.environ.get("SDLC_MCP_PROTOCOL_SSE", "2024-11-05")
# Total attempts per MCP call (1 = the old behaviour, no retry). RUNBOOK-65 observed a single
# `getaddrinfo failed` that was healthy again 3 seconds later; without a retry that blip is written
# up as "we looked and there were no logs", which is the exact confusion this feature must not
# create. Only transport failures that did NOT carry the request are retried, and only because every
# allow-listed MCP operation is a read.
MCP_RETRY_ATTEMPTS = int(os.environ.get("SDLC_MCP_RETRY_ATTEMPTS", "2"))
MCP_RETRY_DELAY = float(os.environ.get("SDLC_MCP_RETRY_DELAY", "3"))

# ---- incident investigator: raw log retention (UAT internal test ONLY) ----
# OFF by default. ON retains the raw log text an investigation read, so a devops tester can click an
# evidence item and check it against the original — evidence you cannot verify is evidence people
# will not trust, which is the whole reason this exists.
#
# What it does NOT change, deliberately: the model still only ever receives the REDACTED packet. Raw
# text goes to a separate owner-scoped store and is fetched by the browser on demand, so it never
# enters the model's context and never re-enters it on later turns of the same conversation. Putting
# raw logs in chat_sessions.json itself would do exactly that, because history_for_agent replays the
# transcript back to the model every turn.
#
# Owner decision 2026-07-30: enable for the devops internal test on UAT. Turn OFF before anything
# resembling production use.
INCIDENT_RAW_LOGS = os.environ.get("SDLC_INCIDENT_RAW_LOGS", "") not in ("", "0", "false", "False")
# Where retained raw text lives. Under webapp_data/ (gitignored) and separate from the session store
# so that "purge the retained logs" is deleting one file, not editing chat history.
INCIDENT_RAW_STORE = os.environ.get(
    "SDLC_INCIDENT_RAW_STORE", os.path.join(os.getcwd(), "webapp_data", "incident_raw.json"))
# Bounded on both axes: oldest entries are dropped past the count, and anything older than the age is
# refused on read AND swept on write. Unbounded retention of production log text is how a testing
# convenience becomes a data-retention finding.
INCIDENT_RAW_MAX_ENTRIES = int(os.environ.get("SDLC_INCIDENT_RAW_MAX_ENTRIES", "200"))
INCIDENT_RAW_TTL_HOURS = int(os.environ.get("SDLC_INCIDENT_RAW_TTL_HOURS", "72"))
# Per-entry line cap. A single unbounded log read could otherwise put tens of MB on disk.
INCIDENT_RAW_MAX_LINES = int(os.environ.get("SDLC_INCIDENT_RAW_MAX_LINES", "500"))

# ---- server ----
HOST = os.environ.get("SDLC_HOST", "127.0.0.1")
PORT = int(os.environ.get("SDLC_PORT", "8765"))

# ---- retrieval upstream (single-entry proxy) ----
# retrieval_service.py serves the arch/impact/coverage pages + their data endpoints. The chat
# reverse-proxies every non-webapp GET to it, so users only ever hit ONE port (this one) and the
# inline views load same-origin. Keep the retrieval service on loopback; point this at it.
RETRIEVAL_UPSTREAM = os.environ.get("RETRIEVAL_UPSTREAM_URL", "http://127.0.0.1:8848").rstrip("/")
