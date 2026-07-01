"""Copilot Responses API provider — POSTs to `<LLM_BASE_URL>/responses`
(the local copilot-api). Default provider.

It converts the app's OpenAI CHAT-STYLE messages to the Responses `input` format
and converts the Responses output back to a chat-style assistant message, and it
attaches usage + copilot-credit metadata under private `_usage` / `_copilot_usage`.

>>> INTERNAL SIDE OWNS THIS FILE. Verify the request/response shapes against your
    local copilot-api and adjust HERE — not in llm.py. That keeps `git pull` clean.

Notes:
  - gpt-5.5 rejected `temperature` in this environment, so it is NOT sent.
  - Private (`_`-prefixed) message keys are stripped before conversion.
"""
import json
import urllib.request
import urllib.error

from .. import config
from . import sanitize_messages


def _to_responses_tools(tools):
    """chat tools ({"type","function":{...}}) -> Responses tools (flat)."""
    out = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _to_responses_input(messages):
    """chat messages -> (instructions, input items). Ignores `_`-prefixed keys."""
    instructions, items = [], []
    for m in sanitize_messages(messages):
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                instructions.append(m["content"])
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id", ""),
                "output": m.get("content") or "",
            })
        elif role == "assistant":
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                })
            if m.get("content"):
                items.append({"role": "assistant", "content": m["content"]})
        else:  # user (or anything else) -> plain input message
            items.append({"role": role or "user", "content": m.get("content") or ""})
    return "\n\n".join(instructions), items


def _from_responses(body):
    """Responses API body -> chat-style assistant message (+ private usage)."""
    content_parts, tool_calls = [], []
    for item in body.get("output") or []:
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content") or []:
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    content_parts.append(part["text"])
        elif itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {"name": item.get("name", ""),
                             "arguments": item.get("arguments") or "{}"},
            })
    content = "".join(content_parts) or body.get("output_text") or None

    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    message["_usage"] = body.get("usage") or {}
    message["_copilot_usage"] = body.get("copilot_usage") or {}
    return message


def chat(messages, tools=None, temperature=0):
    instructions, input_items = _to_responses_input(messages)
    payload = {
        "model": config.LLM_MODEL,
        "input": input_items,
        "max_output_tokens": config.LLM_MAX_TOKENS,
    }
    if instructions:
        payload["instructions"] = instructions
    if tools:
        payload["tools"] = _to_responses_tools(tools)
        payload["tool_choice"] = "auto"
    # temperature intentionally omitted (gpt-5.5 rejects it on this endpoint).

    req = urllib.request.Request(
        config.LLM_BASE_URL.rstrip("/") + "/responses",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if config.LLM_API_KEY:
        req.add_header("Authorization", f"Bearer {config.LLM_API_KEY}")

    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"copilot-api HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"copilot-api unreachable at {config.LLM_BASE_URL}: {e.reason}")

    return _from_responses(body)
