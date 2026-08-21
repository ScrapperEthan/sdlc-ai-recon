"""One ledger entry per tool call: what went in, what came back, and — when it failed — WHICH
argument, in what way.

Why this exists at all. The chat surface used to show a row of chips carrying the tool NAME and
nothing else, and the model was handed `{"error": "'repo'"}` when a required argument was missing —
the bare `str(KeyError)`. Both are the same defect in two places: a failure was recorded as having
happened without recording what it was. Somebody then spends twenty minutes reconstructing, from the
answer text, a fact the process had in its hand and threw away.

Three rules this module exists to keep:

* **The panel and the model read the same ledger.** `record()` stores the EXACT string that was
  appended as the `role: "tool"` message, not a second rendering of the result. Two accounts of one
  call is how a screen ends up saying "Harness did not approve" while the other half of the same
  screen says the connection was refused.
* **`expected` comes from the tool schema or it is not stated.** A made-up expectation makes the
  model confidently change a parameter to another wrong value. `TOOLS` is the only source here; when
  the schema says nothing, `_expected` returns "unknown" and the UI renders `—`.
* **Unclassified failures are ours, never the caller's.** `internal_error` is the fallback, never
  `bad_arguments` — telling a model to fix an argument that was fine is how a run deadlocks, and
  telling a USER to supply something to work around our own wiring bug is worse.

The `failure_class` values are the closed enum agreed in
`docs/specs/ask-fast-retry-plan-zh.md` §2.2. Adding one is a code + test change on purpose; free
text cannot express a new category here.
"""
import json

from . import config, redaction, tools


# The closed enum. Only three of these are assigned by this module (`bad_call_syntax`,
# `bad_arguments`, `internal_error`); the rest exist so a TOOL can declare its own outcome — see
# `classify_result`. A class we do not recognise is downgraded to `internal_error` rather than
# trusted, so the enum stays closed even against a typo in a tool.
FAILURE_CLASSES = frozenset({
    "bad_call_syntax",   # the tool-call arguments were not valid JSON — nothing to name a field with
    "bad_arguments",     # missing / unusable argument. The model can fix this itself
    "empty_result",      # the call SUCCEEDED and found nothing. Not a failure; a conclusion
    "unavailable",       # transport / timeout / connection refused / peer 5xx
    "refused",           # policy, scope, caller_policy — a DELIBERATE limit, never "no data"
    "duplicate",         # this exact call was already made this turn
    "contract_only",     # must go through its own preflight; not retryable from here
    "internal_error",    # unclassified, or our own bug
})

# Who can actually unblock this. The point of the field is that the three answers lead to three
# different next actions, and the expensive mistake is showing a user a gap they cannot close: an
# `internal_error` must never point at them. `empty_result` is absent on purpose — nothing is
# blocked.
WHO_CAN_CLOSE = {
    "bad_call_syntax": "assistant",
    "bad_arguments": "assistant",
    "unavailable": "peer",
    "refused": "config_owner",
    "duplicate": "assistant",
    "contract_only": "us",
    "internal_error": "us",
}

# JSON schema type -> the Python types that satisfy it.
_JSON_TYPES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}
_SCALAR_TYPES = ("string", "integer", "number", "boolean")


def _schema_for(name):
    for item in tools.TOOLS:
        function = item.get("function") or {}
        if function.get("name") == name:
            return function.get("parameters") or {}
    return None


def _expected(spec):
    """What the SCHEMA says about this parameter — never a guess.

    An invented expectation is worse than "unknown": the model acts on it, and changes a wrong
    argument into a differently wrong argument with more confidence than before.
    """
    if not isinstance(spec, dict):
        return "unknown"
    kind = spec.get("type") or "unknown"
    if isinstance(spec.get("enum"), list) and spec["enum"]:
        return f"{kind}, one of: " + "|".join(str(value) for value in spec["enum"])
    return kind


def actual_type(value, present=True):
    """The SHAPE of what arrived — type and length, never the value itself.

    Deliberately value-free: an argument can carry a pasted alert, a customer identifier or a
    production hostname, and this string travels back to the model and into stored session JSON.
    """
    if not present:
        return "absent"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "str(empty)" if not value.strip() else f"str(len={len(value)})"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return f"list(len={len(value)})"
    if isinstance(value, dict):
        return f"object(keys={len(value)})"
    return type(value).__name__


def _is_numeric_text(value):
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _type_problem(kind, value):
    """`fatal` when the value cannot possibly work, `note` when today's dispatch tolerates it.

    The split is the whole no-regression story. A dict where a repo name belongs can only ever end
    in an exception or a silent miss, so it is stopped and named. A `"50"` where an integer belongs
    is what several tools already do `int(...)` on, so it is RECORDED and passed through untouched —
    tightening that would turn calls that work today into failures, which is the one thing this
    change is not allowed to do.
    """
    allowed = _JSON_TYPES.get(kind)
    if allowed is None:                      # schema says nothing -> we say nothing
        return None
    if kind == "integer" and isinstance(value, bool):
        return "note"                        # int(True) == 1; odd, works, recorded
    if isinstance(value, allowed) and not (kind in ("integer", "number") and isinstance(value, bool)):
        return None
    if kind in _SCALAR_TYPES and isinstance(value, (dict, list)):
        return "fatal"
    if kind in ("object", "array"):
        return "fatal"
    if kind in ("integer", "number") and isinstance(value, str) and not _is_numeric_text(value):
        return "fatal"
    return "note"


def check_arguments(name, args):
    """Validate one call's arguments against its own schema.

    Returns ``{"invalid": [...], "notes": [...]}``. `invalid` non-empty means the call must not be
    dispatched; `notes` are observations that change nothing about what runs — they exist so the
    trace panel can explain an odd result instead of leaving somebody to guess.

    Every entry is `{field, problem, expected, actual_type}` — the whitelist from
    `ask-fast-retry-plan-zh.md` §2.3. Field NAMES are public (they are in the schema we published to
    the model); field VALUES are not, and never appear here.
    """
    args = args if isinstance(args, dict) else {}
    schema = _schema_for(name)
    if schema is None:
        # No schema, so nothing to check against — and NOT a reason to refuse. `dispatch` still
        # routes ten pre-consolidation names for the CLI and MCP callers; blocking them here would
        # break a path that works today to catch a name the model was never shown. A name that is
        # genuinely unknown is refused by `dispatch` itself, which declares the class.
        return {"invalid": [],
                "notes": [{"field": "tool", "problem": "no_schema",
                           "expected": "a tool listed in this turn's tool list",
                           "actual_type": f"str(len={len(name or '')})"}]}

    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    invalid, notes = [], []

    for field in required:
        spec = properties.get(field) or {}
        present = field in args
        value = args.get(field)
        problem = None
        if not present or value is None:
            problem = "missing"
        elif isinstance(value, str) and not value.strip():
            # An empty required string reaches the data layer as a lookup for nothing and comes back
            # as "not found" — a wrong answer wearing the clothes of a real one.
            problem = "empty"
        if problem:
            invalid.append({"field": field, "problem": problem, "expected": _expected(spec),
                            "actual_type": actual_type(value, present)})

    for field, value in args.items():
        if field not in properties:
            known = ("one of: " + ", ".join(sorted(properties)) if properties
                     else "this tool takes no arguments")
            notes.append({"field": field, "problem": "unknown_field", "expected": known,
                          "actual_type": actual_type(value)})
            continue
        if any(entry["field"] == field for entry in invalid):
            continue                          # already reported as missing/empty
        spec = properties[field] or {}
        severity = _type_problem(spec.get("type"), value)
        if severity == "fatal":
            invalid.append({"field": field, "problem": "wrong_type", "expected": _expected(spec),
                            "actual_type": actual_type(value)})
        elif severity == "note":
            notes.append({"field": field, "problem": "loose_type", "expected": _expected(spec),
                          "actual_type": actual_type(value)})

    return {"invalid": invalid, "notes": notes}


def parse_arguments(raw):
    """``(args, syntax_problem)`` for the arguments string a tool call carries.

    A tool call whose arguments are not valid JSON used to become `args = {}` silently, and the
    call was dispatched anyway — so a broken JSON string was reported to the model as a MISSING
    FIELD. It would then dutifully resend the same field, which was never the problem: a two-line
    deadlock that costs the whole turn. The syntax failure is now its own class, and it says so
    without naming a field, because at that point we genuinely do not have one.
    """
    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        return {}, None                       # no arguments at all is legitimate
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        return {}, {"failure_class": "bad_call_syntax",
                    "detail": f"invalid JSON at line {error.lineno}, column {error.colno}"}
    if not isinstance(parsed, dict):
        return {}, {"failure_class": "bad_call_syntax",
                    "detail": f"arguments must be a JSON object, got {actual_type(parsed)}"}
    return parsed, None


def classify_result(result):
    """The failure class of a finished call, or ``None`` when it is not a failure.

    A tool may DECLARE its own class (`failure_class` in the result) — that is the migration path:
    the tools that already return explicit argument errors say so, everything else stays as it is.
    An undeclared failure lands on `internal_error` and never on `bad_arguments`, so the model is
    never sent to fix an argument nobody has established was wrong.
    """
    if not isinstance(result, dict):
        return None
    declared = result.get("failure_class")
    if declared:
        return declared if declared in FAILURE_CLASSES else "internal_error"
    if result.get("ok") is False or result.get("error"):
        return "internal_error"
    return None


def _human_message(result):
    """The tool's own words, for the human panel only. Empty when it did not say anything."""
    if not isinstance(result, dict):
        return ""
    for key in ("error", "message", "reason", "detail"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def failure_payload(failure_class, *, invalid=(), notes=(), attempt=1, detail="", error_type=""):
    """What the MODEL is handed back when a call fails before (or instead of) producing a result.

    Field names, schema types, shapes and counts — no argument values, no stack traces, no internal
    module names or paths. `detail` is passed through `redact` because the one place a value can
    still slip in is an exception message we did not write.
    """
    payload = {
        "ok": False,
        "failure_class": failure_class,
        "attempt": attempt,
        "who_can_close": WHO_CAN_CLOSE.get(failure_class, "us"),
    }
    if invalid:
        payload["invalid_arguments"] = [dict(entry) for entry in invalid]
    unknown = [entry["field"] for entry in notes if entry.get("problem") == "unknown_field"]
    if unknown:
        payload["unknown_arguments"] = unknown
    if error_type:
        payload["error_type"] = error_type
    if detail:
        payload["detail"] = redaction.redact(detail)
    payload["guidance"] = _GUIDANCE.get(failure_class, _GUIDANCE["internal_error"])
    return payload


_GUIDANCE = {
    "bad_call_syntax": ("The arguments of that call were not valid JSON, so no field could be read. "
                        "Re-issue the same call with well-formed JSON arguments. Do not assume any "
                        "field was rejected — none was inspected."),
    "bad_arguments": ("Fix the named argument(s) and call the tool again. `expected` comes from the "
                      "tool schema; where it says 'unknown', the schema does not constrain it, so do "
                      "not invent a format."),
    "internal_error": ("This failed on our side. Try a different tool or a different angle; if you "
                       "cannot, say plainly that this evidence could not be retrieved. Do NOT ask "
                       "the user to supply something to work around it."),
    "unavailable": ("The peer system could not be reached. This is not 'no data' — say the evidence "
                    "could not be retrieved, and do not draw a conclusion from its absence."),
    "refused": ("This is a deliberate limit in our configuration, not missing data. Say so; do not "
                "report it as 'nothing found'."),
    "duplicate": "This exact call was already made this turn; its result is above.",
    "contract_only": "This capability runs through its own preflight and cannot be retried here.",
}


def record(*, tool, call_id, seq, iteration, attempt, lane, args, arguments_raw=None,
           invalid=(), notes=(), message=""):
    """A `running` ledger entry, in the shape the browser panel renders and the session store keeps.

    Completed by `finish()`, which is the ONLY place the output block is built — one call, one
    entry, one account of it. The alternative (letting each caller assemble its own finished entry)
    is how the two halves of a screen end up disagreeing about the same run.
    """
    entry = {
        "tool": tool,
        "call_id": call_id,
        "seq": seq,
        "iteration": iteration,
        "attempt": attempt,
        "lane": lane,
        "status": "running",
        "failure_class": None,
        "who_can_close": None,
        "args": args if isinstance(args, dict) else {},
        "invalid": [dict(item) for item in invalid],
        "notes": [dict(item) for item in notes],
        "message": message or "",
        # Did the tool actually RUN? A call rejected on its arguments never reached one, and
        # "never ran" is a different fact from "ran and failed" — collapsing the two is precisely
        # how a screen ends up attributing a peer's refused connection to our own gate.
        "dispatched": False,
        "duration_ms": None,
    }
    # Only set when the JSON was unreadable: it is the one case where the parsed args cannot show
    # what the model actually emitted, and the raw string is what a human needs to see the typo.
    if arguments_raw is not None:
        entry["arguments_raw"] = arguments_raw[:config.TRACE_OUTPUT_CHARS]
    return entry


def _result_chars(result):
    """Size of the UNSHRUNK result, so the panel can say how much the context budget dropped.

    `None` (rendered `—`) when the result would not serialize — an unknown is shown as unknown and
    never as a zero.
    """
    if result is None:
        return None
    try:
        return len(json.dumps(result, ensure_ascii=False))
    except (TypeError, ValueError):
        return None


def finish(entry, *, output_text, result, duration_ms, failure_class=None, message="",
           dispatched=False):
    """Complete a `running` entry in place and return it."""
    entry["status"] = "error" if failure_class else "ok"
    entry["dispatched"] = bool(dispatched)
    entry["failure_class"] = failure_class
    entry["who_can_close"] = WHO_CAN_CLOSE.get(failure_class) if failure_class else None
    entry["message"] = message or entry.get("message") or ""
    entry["duration_ms"] = duration_ms
    entry["output"] = {
        "text": (output_text or "")[:config.TRACE_OUTPUT_CHARS],
        "model_chars": len(output_text or ""),
        "shown_chars": min(len(output_text or ""), config.TRACE_OUTPUT_CHARS),
        "result_chars": _result_chars(result),
    }
    return entry


def human_message_for(result, failure_class):
    """The sentence shown to a person for a finished call — the tool's own words, or nothing.

    Deliberately no invented fallback. A rejected call has no tool to quote (nothing ran), and
    writing "the tool reported a failure" there would state the opposite of what happened; the panel
    reads `dispatched` for that and renders `—` here.
    """
    return _human_message(result)
