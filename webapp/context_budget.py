"""One token budget for a turn, split into named lanes — not a pile of independent caps.

The failure this prevents is running out of context window. Independent per-thing limits cannot
prevent it: a history cap and a per-tool-call cap know nothing about each other, so their worst
cases simply add up, and nothing is watching the total. Here a turn gets ONE budget, the system
prompt and the reserved output are taken off the top, and what is left is divided into lanes:

    total
      - system prompt        (measured, not negotiable — it is whatever it is)
      - output reserve       (kept empty, or the model has no room to answer)
      = working budget
          history            recent conversation turns
          compaction         a summary of turns that did not fit      [lane reserved, see below]
          tools              ALL tool results in this turn, cumulative
          subagent           structured results handed back by a sub-agent   [phase 2]

Two consequences worth stating, because they are the point:

* **The tools lane is cumulative across iterations, not per call.** Eight tool calls at the old
  per-call cap was the real consumer, and nothing tracked the running total. Now each call is
  allowed roughly what is left divided by the calls still to come, so an early greedy result
  cannot starve the rest of the turn.
* **The compaction lane is reserved but not yet filled.** Summarizing dropped turns costs an extra
  model call, latency, and a fresh way to be wrong, so it is deliberately not built yet — but the
  lane exists now so adding it later is filling a hole rather than re-plumbing the budget.

**Tokens are estimated, not counted.** This box is air-gapped and stdlib-only, so there is no
tokenizer: CJK is charged ~1 token/char and everything else ~1 token per 3.6 chars, then multiplied
by a safety factor. The estimate is deliberately pessimistic — the cost of over-estimating is a
slightly shorter answer, the cost of under-estimating is a failed request.

**Nothing here is persisted or persists anything.** ``chat_sessions.json`` keeps recording the full
untrimmed conversation; this only decides what is sent to the model on one turn, so an upgrade can
never retro-actively shorten stored history and a rollback loses nothing.
"""
import json
import math
import re
from copy import deepcopy

from . import config

_MAX_SHRINK_PASSES = 40
_FALLBACK_HEADROOM_TOKENS = 120  # room for the envelope around a last-resort preview
_MIN_TOOL_TOKENS = 200           # a tool result below this is not worth returning at all

# CJK (plus fullwidth punctuation) is roughly one token per character; Latin text and JSON
# punctuation run far more characters per token.
_CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿"
                  r"豈-﫿＀-￯]")


def estimate_tokens(text):
    """Pessimistic token estimate. Swap this for a real tokenizer if one ever ships to the box —
    every budget decision goes through here, so that is the only place that would need to change."""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    raw = cjk + math.ceil(other / 3.6)
    return math.ceil(raw * config.TOKEN_SAFETY_FACTOR)


def _dump(payload):
    return json.dumps(payload, ensure_ascii=False)


class Budget:
    """A turn's token ledger. Construct once per question, then spend against it."""

    def __init__(self, total=None, output_reserve=None, shares=None):
        self.total = int(total if total is not None else config.CONTEXT_TOKENS)
        self.output_reserve = int(
            output_reserve if output_reserve is not None else config.OUTPUT_RESERVE_TOKENS)
        self.shares = dict(shares or config.CONTEXT_LANE_SHARES)
        self.system_tokens = 0
        self.spent = {lane: 0 for lane in self.shares}
        self._lanes = None

    # -- setup -------------------------------------------------------------------------------
    def reserve_system(self, prompt):
        """The system prompt is not a lane — it is subtracted before anything else is divided."""
        self.system_tokens = estimate_tokens(prompt)
        self._lanes = None
        return self.system_tokens

    @property
    def working(self):
        """What is left for lanes once the prompt and the answer have their space."""
        return max(0, self.total - self.system_tokens - self.output_reserve)

    @property
    def lanes(self):
        if self._lanes is None:
            working = self.working
            self._lanes = {lane: int(working * share) for lane, share in self.shares.items()}
        return self._lanes

    def remaining(self, lane):
        return max(0, self.lanes.get(lane, 0) - self.spent.get(lane, 0))

    def charge(self, lane, text):
        used = estimate_tokens(text)
        self.spent[lane] = self.spent.get(lane, 0) + used
        return used

    def report(self):
        """What the budget did this turn — surfaced in the answer's usage block for debugging."""
        return {
            "total": self.total,
            "system": self.system_tokens,
            "output_reserve": self.output_reserve,
            "working": self.working,
            "lanes": dict(self.lanes),
            "spent": dict(self.spent),
            "estimated": True,
            "note": "token counts are ESTIMATES (no tokenizer available offline)",
        }

    # -- spending ----------------------------------------------------------------------------
    def fit_history(self, messages, max_rounds=None):
        """Trim conversation to the history lane. Returns ``(kept, dropped_count)``.

        Bounded twice on purpose — by tokens AND by round count. Tokens protect the request;
        the round cap protects answer quality, because forty turns of context makes the model
        worse at the question actually being asked even when it all technically fits."""
        max_rounds = int(max_rounds if max_rounds is not None else config.HISTORY_MAX_ROUNDS)
        messages = list(messages or [])
        if self.lanes.get("history", 0) <= 0:
            return messages, 0   # lane switched off entirely -> old unbounded behaviour
        lane = self.remaining("history")

        dropped_by_rounds = 0
        if max_rounds > 0 and len(messages) > max_rounds * 2:
            keep_from = len(messages) - max_rounds * 2
            anchor = messages[0:1] if messages and messages[0].get("role") == "user" else []
            trimmed = anchor + messages[keep_from:]
            dropped_by_rounds = len(messages) - len(trimmed)
            messages = trimmed

        def size(message):
            return estimate_tokens(message.get("content") or "") + 8  # role/envelope overhead

        if sum(size(m) for m in messages) <= lane:
            self.spent["history"] = self.spent.get("history", 0) + sum(size(m) for m in messages)
            return messages, dropped_by_rounds

        anchor_index = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
        spent = size(messages[anchor_index]) if anchor_index is not None else 0

        keep = set() if anchor_index is None else {anchor_index}
        for index in range(len(messages) - 1, -1, -1):
            if index == anchor_index:
                continue
            cost = size(messages[index])
            if spent + cost > lane:
                break
            spent += cost
            keep.add(index)

        kept = [messages[i] for i in sorted(keep)]
        self.spent["history"] = self.spent.get("history", 0) + spent
        return kept, dropped_by_rounds + (len(messages) - len(kept))

    def tool_allowance(self, calls_remaining=1):
        """How much this one tool result may use: what's left, shared with the calls still to come.

        An early call that under-spends leaves more for later ones, and a greedy one cannot eat the
        whole turn — the old per-call cap allowed exactly that, eight times over.

        ``lane_left`` is a HARD ceiling — never returned above it, even when that leaves less than
        ``_MIN_TOOL_TOKENS``. An earlier version wrapped the whole expression in one more
        ``max(_MIN_TOOL_TOKENS, ...)``, which meant a nearly-exhausted lane (say 30 tokens left)
        still handed back 200 — a real, if small, hole in the "the total never exceeds the budget"
        guarantee. Once the lane is genuinely spent, callers get whatever is left, even zero;
        ``shrink_tool_result`` already degrades to a self-describing empty-ish preview rather than
        failing when handed a tiny allowance."""
        lane_left = self.remaining("tools")
        if lane_left <= 0:
            return 0
        share = lane_left // max(1, int(calls_remaining))
        return min(lane_left, max(share, _MIN_TOOL_TOKENS))

    def fit_tool_result(self, result, calls_remaining=1):
        text = shrink_tool_result(result, max_tokens=self.tool_allowance(calls_remaining))
        self.charge("tools", text)
        return text

    def fit_subagent_result(self, result):
        """Phase 2: a sub-agent hands back a structured evidence packet, never a transcript."""
        text = shrink_tool_result(result, max_tokens=self.remaining("subagent"))
        self.charge("subagent", text)
        return text


# ---- structure-aware shrinking -------------------------------------------------------------
# A byte slice can cut a JSON document in half, and the model has no way to tell that what it got
# is a fragment rather than the whole truth. These shrink STRUCTURE instead: long strings get an
# explicit marker, long lists get shortened with the loss recorded, and the last-resort path still
# returns valid JSON that announces itself as a preview.

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


def _with_notes(payload, notes):
    """Attach the truncation record. `payload` is always a dict here (a non-dict result is wrapped
    in `_result` before shrinking starts)."""
    if not notes:
        return payload
    annotated = dict(payload)
    annotated["_truncated"] = [notes[path] for path in sorted(notes)]
    return annotated


def shrink_tool_result(result, max_tokens=None, string_cap=None):
    """Serialize `result` into at most `max_tokens` estimated tokens, ALWAYS as valid JSON."""
    max_tokens = int(max_tokens if max_tokens is not None else config.CONTEXT_TOKENS)
    string_cap = int(string_cap if string_cap is not None else config.TOOL_STRING_CAP)

    try:
        text = _dump(result)
    except (TypeError, ValueError):
        return _dump({"_truncated": True, "_note": "tool result was not JSON-serializable",
                      "preview": repr(result)[:2000]})
    if estimate_tokens(text) <= max_tokens:
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
        used = estimate_tokens(text)
        if used <= max_tokens:
            return text
        sites = [site for site in _list_sites(payload) if len(site[3]) > 1]
        if not sites:
            break
        container, key, path, values = max(sites, key=lambda site: len(_dump(site[3])))
        # Aim straight at the budget instead of halving blindly: converges in a pass or two and
        # keeps far more rows. The `len - 1` floor guarantees the loop always makes progress.
        ratio = max_tokens / max(1, used)
        keep = max(1, min(len(values) - 1, int(len(values) * ratio * 0.9)))
        container[key] = values[:keep]
        previous = notes.get(path)
        notes[path] = {"path": path, "kind": "list", "kept": keep,
                       "total": previous["total"] if previous else len(values)}

    text = _dump(_with_notes(payload, notes))
    if estimate_tokens(text) <= max_tokens:
        return text

    # Last resort: the payload is one indivisible lump. Return a VALID envelope that says so,
    # rather than a byte slice the model would mistake for the whole answer.
    preview_chars = max(0, int((max_tokens - _FALLBACK_HEADROOM_TOKENS) * 2))
    return _dump({
        "_truncated": True,
        "_note": (f"tool result exceeded this call's {max_tokens}-token allowance and could not be "
                  f"shrunk structurally. This is a PREVIEW, not the whole result — say so, and "
                  f"narrow the query (a filter, a smaller limit, a shorter time window) instead of "
                  f"drawing conclusions from it."),
        "preview": text[:preview_chars],
    })


def fit_history(messages, budget=None):
    """Back-compat helper for callers that just want a trimmed history without a full ledger."""
    ledger = Budget()
    if budget is not None:
        ledger.shares = {"history": 1.0}
        ledger._lanes = {"history": int(budget)}
    return ledger.fit_history(messages)
