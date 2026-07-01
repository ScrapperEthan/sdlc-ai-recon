"""The agent loop: question -> model (with tools) -> run tools -> answer.
Model-agnostic; the model call goes through the `llm` facade. Token/credit usage
is aggregated across the (possibly several) model calls one question triggers."""
import json
from . import llm, tools, config, llm_usage


def _system_prompt():
    try:
        with open(config.SYSTEM_PROMPT, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ("You are a read-only code assistant for a 400-repo Java system. "
                "Use the tools to find evidence. Cite repo/path:line. "
                "Say 'partial' when something can't be proven from source/data.")


def answer(question, history=None):
    """Return {"answer": str, "tool_trace": [...], "usage": {...}}."""
    messages = [{"role": "system", "content": _system_prompt()}]
    messages += history or []
    messages.append({"role": "user", "content": question})

    trace = []
    usage = llm_usage.empty_usage()
    for _ in range(config.MAX_TOOL_ITERS):
        message = llm.chat(messages, tools.TOOLS)
        llm_usage.add_call(usage, message)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            return {"answer": message.get("content") or "", "tool_trace": trace, "usage": usage}

        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = tools.dispatch(name, args)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
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
    return {"answer": message.get("content") or "", "tool_trace": trace, "usage": usage}
