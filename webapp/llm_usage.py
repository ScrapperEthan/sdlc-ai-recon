"""Aggregate token / Copilot-credit usage across the (possibly several) model
calls one user question triggers (model asks for a tool -> app runs it -> model
answers). Providers attach raw usage under private `_usage` / `_copilot_usage`;
this folds it into one total for the API response and the session store."""


def empty_usage():
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "total_nano_aiu": 0,
        "calls": [],
    }


def add_call(total, message):
    """Fold one model message's usage into `total`. Returns `total`."""
    usage = message.get("_usage") or {}
    copilot = message.get("_copilot_usage") or {}
    call = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "reasoning_tokens": int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0),
        "total_nano_aiu": int(copilot.get("total_nano_aiu") or 0),
        "token_details": copilot.get("token_details") or [],
    }
    for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "total_nano_aiu"):
        total[key] += call[key]
    total["calls"].append(call)
    return total
