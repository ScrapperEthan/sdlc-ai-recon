"""Stable facade — the ONLY model entrypoint the app imports.

    from . import llm
    message = llm.chat(messages, tools.TOOLS)   # OpenAI chat-style message back

DO NOT put provider/protocol code here. Provider implementations live in
`webapp/llm_providers/*`; select one with `LLM_PROVIDER`. Keeping this file small
and stable is what makes internal `git pull` clean: provider/network edits touch
`llm_providers/`, not this shared file. (See the "Merge-Conflict Rule".)
"""
from . import config
from .llm_providers import copilot_responses, openai_chat


def _provider_module():
    provider = config.LLM_PROVIDER
    if provider == "copilot_responses":
        return copilot_responses
    if provider == "openai_chat":
        return openai_chat
    raise RuntimeError(
        f"Unknown LLM_PROVIDER: {provider!r} (expected 'copilot_responses' or 'openai_chat')"
    )


def chat(messages, tools=None, temperature=0):
    """Route to the configured provider; return an OpenAI chat-style message."""
    if config.LLM_MOCK:
        return _mock(messages, tools)

    return _provider_module().chat(messages, tools, temperature)


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
