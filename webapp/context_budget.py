"""What fits in one model call — decided here, at the agent, never at the store.

Two jobs, both previously done by a single blunt instrument (``json.dumps(result)[:CAP]``):

1. **Tool results.** A byte slice can cut a JSON document in half, and the model has no way to tell
   that what it received is a fragment rather than the whole truth — it just reasons over corrupted
   evidence. Everything here instead shrinks *structure*: long strings get an explicit marker, long
   lists get shortened with a recorded ``_truncated`` entry saying what was dropped, and the
   last-resort fallback still returns **valid JSON that announces itself as truncated**. This
   matters far more once log excerpts arrive: a log tail is one enormous string, exactly the shape
   the old byte cut mangled worst.

2. **Conversation history.** ``history_for_agent`` returns every turn ever, unbounded, and each
   question re-sends the lot — so a long session gets steadily slower, costlier, and eventually
   fails outright. Oldest turns are dropped first, while the FIRST user turn is always kept because
   it anchors what the conversation is about.

**Deliberately stateless and read-only.** Nothing here writes anything or changes any persisted
shape: ``chat_sessions.json`` keeps recording the full, untrimmed conversation exactly as before.
Trimming decides what is *sent to the model this turn*, so an upgrade can never retro-actively
shorten someone's stored history, and a rollback loses nothing.
"""
import json
from copy import deepcopy

from . import config

_MAX_SHRINK_PASSES = 40
_FALLBACK_HEADROOM = 400  # room for the envelope around a last-resort preview


def _dump(payload):
    return json.dumps(payload, ensure_ascii=False)


def _cap_strings(node, string_cap, notes, path="$"):
    """Replace over-long strings in place with a marker stating how much was dropped.

    `notes` is keyed by path so one entry describes one location no matter how many passes touch
    it (a list shrunk twice must still report its ORIGINAL total, not the intermediate size)."""
    if isinstance(node, dict):
        items = list(node.items())
    elif isinstance(node, list):
        items = list(enumerate(node))
    else:
        return
    for key, value in items:
        here = f"{path}.{key}" if isinstance(node, dict) else f"{path}[{key}]"
        if isinstance(value, str) and len(value) > string_cap:
            node[key] = value[:string_cap] + f"…[truncated, {len(value)} chars total]"
            notes[here] = {"path": here, "kind": "string", "kept": string_cap, "total": len(value)}
        else:
            _cap_strings(value, string_cap, notes, here)


def _list_sites(node, path="$"):
    """Every list in the structure, as (container, key, path, list) so it can be shrunk in place."""
    sites = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if isinstance(value, list):
                sites.append((node, key, here, value))
            sites.extend(_list_sites(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            here = f"{path}[{index}]"
            if isinstance(value, list):
                sites.append((node, index, here, value))
            sites.extend(_list_sites(value, here))
    return sites


def shrink_tool_result(result, cap=None, string_cap=None):
    """Serialize `result` to at most `cap` characters, ALWAYS as valid JSON.

    Order of attack: cap long strings, then halve the biggest lists, then — only if the payload is
    still oversized — fall back to a preview envelope. Every step that drops something records it
    under ``_truncated`` so the model can say "this list was cut" instead of quietly treating a
    partial list as complete."""
    cap = int(cap if cap is not None else config.TOOL_RESULT_CAP)
    string_cap = int(string_cap if string_cap is not None else config.TOOL_STRING_CAP)

    try:
        text = _dump(result)
    except (TypeError, ValueError):
        return _dump({"_truncated": True, "_note": "tool result was not JSON-serializable",
                      "preview": repr(result)[:max(0, cap - _FALLBACK_HEADROOM)]})
    if len(text) <= cap:
        return text

    payload = deepcopy(result)
    # A top-level list has no dict to hang `_truncated` on and would otherwise be invisible to
    # _list_sites — wrap it so it can be shrunk like any other list.
    if not isinstance(payload, dict):
        payload = {"_result": payload}
    notes = {}
    _cap_strings(payload, string_cap, notes)

    for _ in range(_MAX_SHRINK_PASSES):
        text = _dump(_with_notes(payload, notes))
        if len(text) <= cap:
            return text
        sites = [site for site in _list_sites(payload) if len(site[3]) > 1]
        if not sites:
            break
        container, key, path, values = max(sites, key=lambda site: len(_dump(site[3])))
        # Aim straight at the budget instead of halving blindly: converges in a pass or two and
        # keeps far more rows. The `len - 1` floor guarantees the loop always makes progress.
        ratio = cap / max(1, len(text))
        keep = max(1, min(len(values) - 1, int(len(values) * ratio * 0.9)))
        container[key] = values[:keep]
        # One note per path, and `total` must stay the ORIGINAL length: a list shrunk twice would
        # otherwise report the intermediate size as the total and understate what was lost.
        previous = notes.get(path)
        notes[path] = {"path": path, "kind": "list", "kept": keep,
                       "total": previous["total"] if previous else len(values)}

    text = _dump(_with_notes(payload, notes))
    if len(text) <= cap:
        return text

    # Last resort: the payload is one indivisible lump. Return a VALID envelope that says so,
    # rather than a byte slice the model would mistake for the whole answer.
    return _dump({
        "_truncated": True,
        "_note": (f"tool result exceeded the {cap}-char context budget and could not be shrunk "
                  f"structurally. This is a PREVIEW, not the whole result — say so, and narrow the "
                  f"query (add a filter, a smaller limit, or a narrower time window) instead of "
                  f"drawing conclusions from it."),
        "preview": text[:max(0, cap - _FALLBACK_HEADROOM)],
    })


def _with_notes(payload, notes):
    """Attach the truncation record without disturbing the tool's own shape. `payload` is always a
    dict by this point (a non-dict result is wrapped in `_result` before shrinking starts)."""
    if not notes:
        return payload
    annotated = dict(payload)
    annotated["_truncated"] = [notes[path] for path in sorted(notes)]
    return annotated


def fit_history(messages, budget=None):
    """Trim a conversation to `budget` characters. Returns ``(kept, dropped_count)``.

    Keeps the FIRST user turn (it says what the conversation is about — drop it and the model
    starts answering a different question) plus as many of the most recent turns as fit. Does not
    summarize: a summarization pass costs an extra model call, adds latency and invents a new way
    to be wrong, none of which is worth it while the budget is generous enough that ordinary
    sessions never reach it."""
    budget = int(budget if budget is not None else config.HISTORY_CHAR_BUDGET)
    messages = list(messages or [])
    if budget <= 0:
        return messages, 0

    def size(message):
        return len(message.get("content") or "") + 32  # role/JSON overhead, roughly

    if sum(size(m) for m in messages) <= budget:
        return messages, 0

    anchor_index = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
    anchor = messages[anchor_index] if anchor_index is not None else None
    spent = size(anchor) if anchor is not None else 0

    tail, seen = [], set()
    for index in range(len(messages) - 1, -1, -1):
        if index == anchor_index:
            continue
        cost = size(messages[index])
        if spent + cost > budget:
            break
        spent += cost
        tail.append(index)
        seen.add(index)

    keep = sorted(seen | ({anchor_index} if anchor_index is not None else set()))
    return [messages[i] for i in keep], len(messages) - len(keep)
