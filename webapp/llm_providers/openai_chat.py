"""Standard OpenAI-compatible `/chat/completions` provider.

Use it with `LLM_PROVIDER=openai_chat` for any endpoint that speaks the classic
Chat Completions API. urllib only (no `openai` package; air-gap friendly)."""
import json
import urllib.request
import urllib.error

from .. import config
from . import sanitize_messages


def chat(messages, tools=None, temperature=0):
    payload = {
        "model": config.LLM_MODEL,
        "messages": sanitize_messages(messages),
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
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
        raise RuntimeError(f"LLM HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM unreachable at {config.LLM_BASE_URL}: {e.reason}")

    message = body["choices"][0]["message"]
    # normalise usage to the same field names the copilot provider uses
    u = body.get("usage") or {}
    message["_usage"] = {
        "input_tokens": u.get("input_tokens", u.get("prompt_tokens", 0)),
        "output_tokens": u.get("output_tokens", u.get("completion_tokens", 0)),
        "total_tokens": u.get("total_tokens", 0),
    }
    return message
