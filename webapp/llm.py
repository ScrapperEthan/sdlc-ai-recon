"""Stable facade — the ONLY model entrypoint the app imports.

    from . import llm
    message = llm.chat(messages, tools.TOOLS)   # OpenAI chat-style message back

DO NOT put provider/protocol code here. Provider implementations live in
`webapp/llm_providers/*`; select one with `LLM_PROVIDER`. Keeping this file small
and stable is what makes internal `git pull` clean: provider/network edits touch
`llm_providers/`, not this shared file. (See the "Merge-Conflict Rule".)
"""
from . import config
from .llm_providers import copilot_responses, openai_chat, github_copilot_direct


class LlmAuthError(RuntimeError):
    """Provider-neutral 401: the credential/token was rejected. Callers (server.py) catch THIS, not
    any `llm_providers/*` exception type -- see `_raise_normalized_provider_error` below, the one
    place provider-specific errors are translated so nothing downstream needs to import a provider
    module just to handle its errors."""


class LlmForbiddenError(RuntimeError):
    """Provider-neutral 403: the credential is valid but lacks access/entitlement."""


class LlmRateLimitError(RuntimeError):
    """Provider-neutral 429. `retry_after` (seconds, optional) is carried through when the
    provider supplied one -- `getattr(error, "retry_after", None)` so this works whether or not the
    underlying provider exception has grown that attribute yet."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _raise_normalized_provider_error(error):
    """Translate a `github_copilot_direct` (or any future provider's) auth/forbidden/rate-limit
    exception into the provider-neutral types above; anything else is re-raised unchanged.

    This is the ONLY place in the external-owned codebase that imports a provider exception type --
    `server.py` must never do so (see docs/specs/copilot-dynamic-model-selector-external-diff-zh.md
    §6.4): a provider being refactored, split, or lazily imported should never be able to break
    webapp startup, and every provider's errors reach callers through the SAME small vocabulary."""
    if isinstance(error, github_copilot_direct.CopilotRateLimitError):
        raise LlmRateLimitError(str(error), retry_after=getattr(error, "retry_after", None)) from None
    if isinstance(error, github_copilot_direct.CopilotAuthError):
        raise LlmAuthError(str(error)) from None
    if isinstance(error, github_copilot_direct.CopilotForbiddenError):
        raise LlmForbiddenError(str(error)) from None
    raise error


def _call_provider_chat(messages, tools, temperature):
    """provider.chat() with normalized errors -- the ONE place both `chat()` and `chat_stream()`'s
    blocking fallback path go through, so a raw provider exception never reaches a caller via either
    route (chat_stream's own internal fallback used to call `provider.chat` directly, bypassing
    whatever normalization `chat()` did)."""
    try:
        return _provider_module().chat(messages, tools, temperature)
    except Exception as error:  # noqa: BLE001 -- classify or re-raise, never swallow
        _raise_normalized_provider_error(error)


def _provider_module():
    """Override-aware: `config.LLM_PROVIDER` resolves per-request (see config.py), so a token-mode
    caller can get `github_copilot_direct` in the same process where everyone else gets the env
    default. No token/proxy/cert code here -- that lives in `llm_providers/github_copilot_direct.py`."""
    provider = config.LLM_PROVIDER
    if provider == "copilot_responses":
        return copilot_responses
    if provider == "openai_chat":
        return openai_chat
    if provider == "github_copilot_direct":
        return github_copilot_direct
    raise RuntimeError(
        f"Unknown LLM_PROVIDER: {provider!r} "
        "(expected 'copilot_responses', 'openai_chat', or 'github_copilot_direct')"
    )


def models():
    """List models the current (override-aware) provider exposes, for the model-picker UI.

    Returns `{"models": [{"id": "...", "label": "..."}, ...], "default_model": "..."}`. Forwards to
    the provider's own `models()` -- no URL/token/proxy/payload logic here, same rule as
    `chat`/`chat_stream` above. A provider that hasn't implemented `models()` yet degrades to a
    single-entry list built from its configured model, so callers never have to special-case it."""
    if config.LLM_MOCK:
        return _mock_models()

    provider = _provider_module()
    lister = getattr(provider, "models", None)
    if lister:
        try:
            return lister()
        except Exception as error:  # noqa: BLE001 -- classify or re-raise, never swallow
            _raise_normalized_provider_error(error)
    model = config.LLM_MODEL
    return {"models": [{"id": model, "label": model}] if model else [], "default_model": model}


def chat(messages, tools=None, temperature=0):
    """Route to the configured provider; return an OpenAI chat-style message."""
    if config.LLM_MOCK:
        return _mock(messages, tools)

    return _call_provider_chat(messages, tools, temperature)


def chat_stream(messages, tools=None, temperature=0):
    """One model turn as a generator of ('delta', text) chunks then ('final', message).

    True token streaming only when `LLM_STREAM` is on AND the provider supports it; otherwise (and
    on ANY streaming failure) it degrades to a single blocking `chat` yielded as one ('final', …),
    so callers get identical behaviour to `chat` when streaming is off or unavailable."""
    if config.LLM_MOCK:
        yield ("final", _mock(messages, tools))
        return

    provider = _provider_module()
    streamer = getattr(provider, "chat_stream", None)
    if config.LLM_STREAM and streamer:
        emitted = False
        try:
            for item in streamer(messages, tools, temperature):
                if item and item[0] == "delta":
                    emitted = True
                yield item
            return
        except Exception:  # noqa: BLE001 — endpoint can't stream / mid-stream drop (broad on
                           # purpose: ANY streaming failure falls back to the blocking call, which
                           # is itself normalized via _call_provider_chat below)
            if emitted:
                # Already showed partial text; finish with a clean blocking result so the answer
                # is complete (rare: a mid-stream connection drop).
                yield ("final", _call_provider_chat(messages, tools, temperature))
                return
            # nothing shown yet -> clean fall-through to the blocking call below

    yield ("final", _call_provider_chat(messages, tools, temperature))


def stream_text(message):
    """Yield assistant text in chunks, using provider streaming when available."""
    if not config.LLM_MOCK:
        provider = _provider_module()
        streamer = getattr(provider, "stream_text", None)
        if streamer and message.get("_stream_handle"):
            yield from streamer(message)
            return

    text = message.get("content") or ""
    for i in range(0, len(text), 24):
        yield text[i:i + 24]


def _mock_models():
    model = config.LLM_MODEL or "mock-model"
    return {"models": [{"id": model, "label": model}], "default_model": model}


def _mock(messages, tools):
    """Deterministic loop so the app runs with no model: call `hubs` once, then answer."""
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return {"role": "assistant",
                "content": "[MOCK] Tools work. Set LLM_PROVIDER + point LLM_BASE_URL at the model "
                           "to get real answers.\nAbove is a live `hubs` result from the retrieval layer."}
    if tools:
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": "mock1", "type": "function",
                                "function": {"name": "hubs", "arguments": "{\"top\": 5}"}}]}
    return {"role": "assistant", "content": "[MOCK] no tools available."}
