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

    def add_text(value):
        if isinstance(value, str) and value:
            content_parts.append(value)

    def normalize_arguments(value):
        if isinstance(value, str):
            return value
        return json.dumps(value or {}, ensure_ascii=False)

    output_items = body.get("output") or []
    if isinstance(output_items, str):
        add_text(output_items)
        output_items = []
    elif isinstance(output_items, dict):
        output_items = [output_items]

    for item in output_items:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            content = item.get("content") or []
            if isinstance(content, str):
                add_text(content)
                continue
            for part in content:
                if isinstance(part, str):
                    add_text(part)
                elif isinstance(part, dict):
                    if part.get("type") in ("output_text", "text", "summary_text"):
                        add_text(part.get("text") or part.get("content") or "")
                    else:
                        add_text(part.get("text") or "")
        elif itype in ("output_text", "text"):
            add_text(item.get("text") or item.get("content") or "")
        elif itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {"name": item.get("name", ""),
                             "arguments": normalize_arguments(item.get("arguments"))},
            })
    output_text = body.get("output_text")
    if isinstance(output_text, list):
        output_text = "".join(str(part) for part in output_text)
    elif isinstance(output_text, dict):
        output_text = output_text.get("text") or output_text.get("content")
    content = "".join(content_parts) or output_text or None

    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = body.get("usage") or {}
    message["_usage"] = {
        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
        "total_tokens": usage.get("total_tokens", 0),
        "output_tokens_details": usage.get("output_tokens_details") or {},
    }
    message["_copilot_usage"] = body.get("copilot_usage") or usage.get("copilot_usage") or {}
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
