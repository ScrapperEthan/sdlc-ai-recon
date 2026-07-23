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


def chat(messages, tools=None, temperature=0):
    """Route to the configured provider; return an OpenAI chat-style message."""
    if config.LLM_MOCK:
        return _mock(messages, tools)

    return _provider_module().chat(messages, tools, temperature)


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
        except Exception:  # noqa: BLE001 — endpoint can't stream / mid-stream drop
            if emitted:
                # Already showed partial text; finish with a clean blocking result so the answer
                # is complete (rare: a mid-stream connection drop).
                yield ("final", provider.chat(messages, tools, temperature))
                return
            # nothing shown yet -> clean fall-through to the blocking call below

    yield ("final", provider.chat(messages, tools, temperature))


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
