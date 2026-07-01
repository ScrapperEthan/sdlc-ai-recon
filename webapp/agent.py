"""The agent loop: question -> model (with tools) -> run tools -> answer.
Model-agnostic; the model call goes through the `llm` facade. Token/credit usage
is aggregated across the (possibly several) model calls one question triggers."""
import json
from retriever import citations
from . import llm, tools, config, llm_usage


def _system_prompt():
    try:
        with open(config.SYSTEM_PROMPT, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ("You are a read-only code assistant for a 400-repo Java system. "
                "Use the tools to find evidence. Cite repo/path:line. "
                "Say 'partial' when something can't be proven from source/data.")


def _tool_names():
    return {item.get("function", {}).get("name") for item in tools.TOOLS}


def _json_from_text(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _fallback_tool_calls(message):
    """Parse text JSON tool actions for providers without native tool calling."""
    data = _json_from_text(message.get("content"))
    if not data:
        return []

    if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
        candidates = data["tool_calls"]
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = [data]

    valid_names = _tool_names()
    calls = []
    for index, item in enumerate(candidates, 1):
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name") or fn.get("tool") or fn.get("action")
        if name not in valid_names:
            continue
        args = fn.get("arguments", fn.get("args", {}))
        if isinstance(args, str):
            arg_text = args
        else:
            arg_text = json.dumps(args or {}, ensure_ascii=False)
        calls.append({
            "id": item.get("id") or f"text_tool_{index}",
            "type": "function",
            "function": {"name": name, "arguments": arg_text},
        })
    return calls


def answer(question, history=None):
    """Return {"answer": str, "tool_trace": [...], "usage": {...}}."""
    answer_text = ""
    trace = []
    usage = llm_usage.empty_usage()
    citation_report = citations.verify("")
    for event in answer_events(question, history):
        if event.get("type") == "done":
            answer_text = event.get("answer") or ""
            trace = event.get("tool_trace") or []
            usage = event.get("usage") or usage
            citation_report = event.get("citations") or citations.verify(answer_text)
    return {
        "answer": answer_text,
        "tool_trace": trace,
        "usage": usage,
        "citations": citation_report,
    }


def answer_events(question, history=None):
    """Yield chat protocol events; finish with a terminal done event."""
    messages = [{"role": "system", "content": _system_prompt()}]
    messages += history or []
    messages.append({"role": "user", "content": question})

    trace = []
    usage = llm_usage.empty_usage()
    for _ in range(config.MAX_TOOL_ITERS):
        message = llm.chat(messages, tools.TOOLS)
        llm_usage.add_call(usage, message)
        calls = message.get("tool_calls") or _fallback_tool_calls(message)
        if calls and not message.get("tool_calls"):
            message = dict(message)
            message["content"] = None
            message["tool_calls"] = calls
        messages.append(message)
        if not calls:
            answer_text = message.get("content") or ""
            for chunk in llm.stream_text(message):
                yield {"type": "token", "text": chunk}
            yield {
                "type": "done",
                "answer": answer_text,
                "tool_trace": trace,
                "usage": usage,
                "citations": citations.verify(answer_text),
            }
            return

        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_start", "name": name, "args": args}
            try:
                result = tools.dispatch(name, args)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            yield {"type": "tool_end", "name": name}
            trace.append({"tool": name, "args": args})
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False)[:config.TOOL_RESULT_CAP],
            })

    # tool budget exhausted — force a final answer
    messages.append({"role": "user",
                     "content": "Answer now with what you have; mark anything unverified as partial."})
    message = llm.chat(messages)
    llm_usage.add_call(usage, message)
    answer_text = message.get("content") or ""
    for chunk in llm.stream_text(message):
        yield {"type": "token", "text": chunk}
    yield {
        "type": "done",
        "answer": answer_text,
        "tool_trace": trace,
        "usage": usage,
        "citations": citations.verify(answer_text),
    }
