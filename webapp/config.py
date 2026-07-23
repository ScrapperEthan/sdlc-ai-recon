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


# ---- assistant behaviour ----
SYSTEM_PROMPT = os.environ.get(
    "SDLC_SYSTEM_PROMPT", os.path.join(os.getcwd(), "prompts", "qa-system-prompt.md")
)
MAX_TOOL_ITERS = int(os.environ.get("SDLC_MAX_TOOL_ITERS", "8"))
TOOL_RESULT_CAP = int(os.environ.get("SDLC_TOOL_RESULT_CAP", "12000"))
SESSION_STORE = os.environ.get(
    "SDLC_SESSION_STORE", os.path.join(os.getcwd(), "webapp_data", "chat_sessions.json")
)
# Per-user LLM route registry (token -> their loopback endpoint). Gitignored like the session store.
LLM_ROUTES_STORE = os.environ.get(
    "SDLC_LLM_ROUTES", os.path.join(os.getcwd(), "webapp_data", "llm_routes.json")
)
# Safety: a registered endpoint must be loopback (each user's LLM is reached via THEIR server-side
# reverse-tunnel port, always 127.0.0.1:<port>). This also blocks SSRF to arbitrary internal hosts.
# Set to "1" only if a deployment deliberately uses non-loopback connector hosts.
LLM_ALLOW_NONLOOPBACK = os.environ.get("SDLC_LLM_ALLOW_NONLOOPBACK", "") not in ("", "0", "false", "False")

# ---- server ----
HOST = os.environ.get("SDLC_HOST", "127.0.0.1")
PORT = int(os.environ.get("SDLC_PORT", "8765"))

# ---- retrieval upstream (single-entry proxy) ----
# retrieval_service.py serves the arch/impact/coverage pages + their data endpoints. The chat
# reverse-proxies every non-webapp GET to it, so users only ever hit ONE port (this one) and the
# inline views load same-origin. Keep the retrieval service on loopback; point this at it.
RETRIEVAL_UPSTREAM = os.environ.get("RETRIEVAL_UPSTREAM_URL", "http://127.0.0.1:8848").rstrip("/")
