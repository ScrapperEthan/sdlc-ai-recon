"""LLM providers behind the stable `webapp/llm.py` facade.

Each provider exposes `chat(messages, tools=None, temperature=0)` and returns an
OpenAI CHAT-STYLE assistant message:

    {"role": "assistant", "content": str | None, "tool_calls": [...]?}

optionally carrying private usage metadata under keys that start with "_"
(e.g. "_usage", "_copilot_usage"). Those private keys must NEVER be sent back to
a model — see `sanitize_messages`.

Put provider-specific / external-network changes in these files, NOT in llm.py.
"""


def sanitize_messages(messages):
    """Drop private (`_`-prefixed) keys before sending messages to any model."""
    return [{k: v for k, v in m.items() if not str(k).startswith("_")} for m in messages]
