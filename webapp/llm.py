"""
================  THE MODEL SEAM — Codex wires GPT-5.5 here  ================

`chat(messages, tools)` is the ONLY function in the whole app that talks to the
model. It targets an OpenAI-compatible /chat/completions endpoint via urllib
(no `openai` package needed — works air-gapped).

Codex, please confirm/adjust ONLY this file:
  - config.LLM_BASE_URL   e.g. https://<internal-gpt-5.5-host>/v1
  - auth header           default: `Authorization: Bearer <LLM_API_KEY>`
                          (change here if your gateway uses a different scheme)
  - config.LLM_MODEL      the model id to send
  - tool calling          we send OpenAI `tools` + read `message.tool_calls`.
                          If the endpoint doesn't support function-calling,
                          tell me and I'll switch agent.py to the prompt-based
                          fallback (no other file changes needed).

Set LLM_MOCK=1 to run the whole app WITHOUT a model (canned loop) so the UI and
tools can be tested before the model is connected.
============================================================================
"""
import json
import urllib.request
import urllib.error
from . import config


def chat(messages, tools=None, temperature=0):
    """Return the assistant message dict: {"role","content","tool_calls"?}."""
    if config.LLM_MOCK:
        return _mock(messages, tools)

    payload = {"model": config.LLM_MODEL, "messages": messages, "temperature": temperature}
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
    return body["choices"][0]["message"]


def _mock(messages, tools):
    """Deterministic loop so the app runs with no model: call `hubs` once, then answer."""
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return {"role": "assistant",
                "content": "[MOCK] Tools work. Connect GPT-5.5 in webapp/llm.py to get real answers.\n"
                           "Above is a live `hubs` result from the retrieval layer."}
    if tools:
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": "mock1", "type": "function",
                                "function": {"name": "hubs", "arguments": "{\"top\": 5}"}}]}
    return {"role": "assistant", "content": "[MOCK] no tools available."}
