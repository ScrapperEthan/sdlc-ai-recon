"""Reading response bodies we did not write.

Everything here takes something the colleagues' servers sent back and turns it into a shape this
side can reason about. Nothing here opens a socket, decides what to ask, or writes a packet — which
is why it is worth having on its own: it is the layer four rounds of intranet review kept finding
defects in, and every one of those defects was reproducible from a captured body alone.

The rule the whole module follows (intranet, 2026-07-31, after three parsers were wrong the same
way):

* a body that parses as JSON is read STRUCTURALLY and never with a regex;
* which fields hold the data is declared by the intranet in `config/mcp_tools.json` under the
  operation's `response` key — the shapes below are only the fallback, so a field rename is a config
  edit on the box, not an external push;
* a JSON body no declared field fits FAILS CLOSED — no lines, no app names, and a stated reason
  naming what was looked for. The alternative is the bug that was found: JSON metadata reported as
  production evidence, which is worse than no answer;
* only a body that is not JSON at all takes the legacy text path.

`describe_shape` / `describe_response` are the other half of that seam: they answer "what does your
tool actually return?" from inside the intranet, in field names and types, with no value ever
appearing — which is how the guessing stops without a log line travelling.
"""
import json
import math
import os
import re
from datetime import datetime

from . import mcp_registry
from .redaction import redact, sanitize_packet


# ---- reading THEIR response bodies -----------------------------------------------------------
# Three parsers were wrong in the same way (intranet, 2026-07-31): each treated a structured JSON
# body as if it were text. `log.read` split the JSON source and reported 11 lines for a 2-line
# response; `log.list_apps` split the whole body on punctuation, so `entries`, `entry_type`, `name`
# and a `README.txt` sitting beside the app all became "app names".
#
# The rule now, the same seam the argument names already follow:
#
#   * a body that parses as JSON is read STRUCTURALLY and never with a regex;
#   * which fields hold the data is declared by the intranet in `config/mcp_tools.json` under the
#     operation's `response` key — the shapes below are only the fallback, so a field rename is a
#     config edit on the box, not an external push;
#   * a JSON body no declared field fits FAILS CLOSED — no lines, no app names, and a stated reason
#     naming what was looked for. The alternative is the bug that was found: JSON metadata reported
#     as production evidence, which is worse than no answer;
#   * only a body that is not JSON at all takes the legacy text path.
#
# A value may be a dotted path (`data.entries`) so a nested body needs no code change either.
RESPONSE_SHAPES = {
    "log.read": {
        "lines": ("lines", "log_lines", "records", "entries", "results", "items", "matches", "data"),
        "line_text": ("line", "text", "message", "content", "log", "raw"),
        # Their own count, reported next to ours so a disagreement is visible rather than assumed.
        "count": ("line_count", "total_lines", "matched_lines", "total", "count"),
        # HOW they actually read the file, which is not always how we asked. The intranet's
        # 2026-08-04 live probe: `mode=keyword` with a keyword that cannot match returns the last N
        # lines with `retrieval_method: tail` rather than zero rows. Their response was already
        # honest about it; we were the ones not looking. See `validate_log_read_semantics`.
        "retrieval_method": ("retrieval_method", "read_method", "method", "mode_used", "read_mode"),
    },
    "log.list_apps": {
        "entries": ("entries", "apps", "applications", "items", "results", "data"),
        "name": ("name", "app", "app_name"),
        # An app is a DIRECTORY on the log host; a file beside it is not an app. Enforced whenever
        # the batch actually carries a kind field — see `extract_app_names` for why not always.
        "kind": ("entry_type", "type", "kind"),
        "kind_value": "dir",
    },
    "log.search_files": {
        "entries": ("files", "entries", "items", "results", "matches", "data"),
        "name": ("file", "file_name", "filename", "name", "path"),
        "kind": ("entry_type", "type", "kind"),
        # Inverted here: exclude what declares itself a directory, rather than require "file". The
        # shapes already verified on the box carry no kind field at all, and a `.log` name is
        # required regardless, so demanding a field nobody has observed would refuse real answers.
        "kind_exclude": ("dir", "directory", "folder"),
    },
}


def response_shape(operation):
    """Field names for one operation's response body: intranet config first, built-in as fallback."""
    shape = dict(RESPONSE_SHAPES.get(operation) or {})
    declared = (mcp_registry.operations().get(operation) or {}).get("response")
    if not isinstance(declared, dict):
        return shape
    for key, value in declared.items():
        if str(key).startswith("_"):
            continue
        if key == "kind_value":
            shape[key] = value if isinstance(value, str) else None
        elif value is None:
            shape[key] = ()             # "this server has no such field" — an explicit answer
        elif isinstance(value, str):
            shape[key] = (value,)
        elif isinstance(value, (list, tuple)):
            shape[key] = tuple(str(v) for v in value)
    return shape


def _decode(text, structured=None):
    """The response body as data, or None when it is not JSON at all.

    `structuredContent` is the server's own typed answer and wins when present. A bare JSON scalar
    counts as text, not structure: a log body of `null` or `42` must not be mistaken for a shape.
    """
    if isinstance(structured, (dict, list)):
        return structured
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, (dict, list)) else None


def _dig(body, path):
    """`data.entries` -> body["data"]["entries"], or None. Never walks looking for a match."""
    node = body
    for part in str(path).split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _rows(body, keys):
    """The declared list inside a JSON body, or None when no declared field holds one.

    None is not "empty". It means the body had a shape we do not know how to read, which must fail
    closed — scraping tokens out of it is exactly the defect this replaces.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in keys:
            value = _dig(body, key)
            if isinstance(value, list):
                return value
    return None


def _shape_error(operation, field, keys, extra=""):
    return (f"{operation} returned a JSON body with no recognised {field} field (looked for: "
            f"{', '.join(keys) or 'nothing declared'}). {extra}Nothing was taken from it — reading "
            f"a structured body as text reports JSON metadata as evidence. Declare the real field "
            f"under operations['{operation}'].response.{field} in the intranet's mcp_tools.json.")


def extract_log_lines(text, structured=None, operation="log.read"):
    """A log response -> (lines, reported_count, error). `lines is None` means fail closed.

    `reported_count` is the server's own count when it states one, kept so "they said 200, we can
    read 50" is visible instead of silently becoming 50.
    """
    shape = response_shape(operation)
    body = _decode(text, structured)
    if body is None:
        # Plain log text: the legacy shape, and the only case where splitting is correct.
        return [line for line in (text or "").splitlines() if line.strip()], None, ""
    rows = _rows(body, shape.get("lines") or ())
    if rows is None:
        return None, None, _shape_error(operation, "lines", shape.get("lines") or ())
    reported = None
    if isinstance(body, dict):
        for key in shape.get("count") or ():
            value = _dig(body, key)
            if isinstance(value, int) and not isinstance(value, bool):
                reported = value
                break
    text_keys = shape.get("line_text") or ()
    lines = []
    for row in rows:
        if isinstance(row, str):
            if row.strip():
                lines.append(row)
            continue
        if isinstance(row, dict):
            value = next((row[k] for k in text_keys if isinstance(row.get(k), str)), None)
            if value is None:
                return None, None, _shape_error(
                    operation, "line_text", text_keys,
                    extra="The line container was found but its entries carry no known text field. ")
            if value.strip():
                lines.append(value)
            continue
        return None, None, _shape_error(
            operation, "lines", shape.get("lines") or (),
            extra=f"An entry was a {type(row).__name__}, not a line. ")
    return lines, reported, ""


# How a `log.read` actually came back, most-trusted first. `keyword_match` is the ONLY value that
# licenses the word "hit"; everything else is context or a refusal.
READ_OUTCOMES = ("keyword_match", "time_context", "tail_context", "no_match",
                 "semantic_downgrade", "unreadable")

# Their names for "I did not search, I just took the end of the file". Configurable for the usual
# reason — this is vocabulary on their side — but the DEFAULT is deliberately broad, because the
# failure mode of an unrecognised value is the dangerous direction: an unknown method with a keyword
# we cannot confirm locally still ends up as `no_match`, never as a hit.
TAIL_METHODS = ("tail", "last", "end", "recent")
CONTEXT_METHODS = ("alert_time_backtrack", "backtrack", "range", "window", "time_range")


def _declared_methods(operation, key, fallback):
    declared = ((mcp_registry.operations().get(operation) or {}).get("request") or {}).get(key)
    if isinstance(declared, str):
        return (declared.lower(),)
    if isinstance(declared, (list, tuple)) and declared:
        return tuple(str(item).lower() for item in declared)
    return fallback


def literal_matches(lines, keyword):
    """The lines that actually CONTAIN the keyword, checked here rather than taken on trust.

    Case-insensitive substring, nothing cleverer: this is a verification, and a fuzzy verification
    that accepts near-misses verifies nothing. An empty keyword matches nothing rather than
    everything — "no keyword was asked for" must not read as "every line matched".
    """
    needle = (keyword or "").strip().casefold()
    if not needle:
        return []
    return [line for line in lines if needle in str(line).casefold()]


def validate_log_read_semantics(out, requested_mode="", requested_keyword="",
                                operation="log.read"):
    """Did this read actually answer the question we asked?

    The defect this exists for (intranet live probe, 2026-08-04): asking `log.read` for a keyword
    that cannot match does NOT return zero rows. It silently returns the last N lines with
    `retrieval_method: tail` — and the previous code accepted any non-empty response as
    "keyword hit: N lines". A server-side fallback was being renamed, on our side, into evidence.
    Their response was honest the whole time; we were not reading the field that said so.

    Two independent checks, because either alone can be fooled:

    * **What they say they did.** `retrieval_method` from the body. A `tail` when we asked for a
      keyword is a `semantic_downgrade` — the query ran, it just answered a different question.
    * **What the lines actually contain.** Checked locally, so a server that reports the right
      method but returns the wrong rows still cannot produce a false hit. This one does not depend
      on their field names, their vocabulary, or their honesty, which is why it is the one that
      decides.

    Returns a dict rather than a boolean because the caller has to report the DIFFERENCE: "we looked
    and there were no matching lines" and "we asked for a search and got the tail of the file
    instead" lead to opposite next actions, and collapsing them is how a wiring gap becomes
    "the logs were clean".

    Shared by the investigator and the MCP console on purpose. The console showing
    `retrieval_method=tail` while the product caller still consumed it as a keyword hit is exactly
    the split the intranet warned about.
    """
    text = (out or {}).get("text") or ""
    structured = (out or {}).get("structured")
    body = _decode(text, structured)
    lines, reported, error = extract_log_lines(text, structured, operation)

    shape = response_shape(operation)
    actual = ""
    for key in (shape.get("retrieval_method") or ()):
        value = _dig(body, key) if isinstance(body, dict) else None
        if isinstance(value, str) and value.strip():
            actual = value.strip()
            break

    wanted_keyword = (requested_keyword or "").strip()
    matched = literal_matches(lines or [], wanted_keyword)
    lowered = actual.lower()
    is_tail = lowered in _declared_methods(operation, "tail_methods", TAIL_METHODS)
    is_context = lowered in _declared_methods(operation, "context_methods", CONTEXT_METHODS)
    # We sent a keyword, so a tail response is a downgrade — regardless of the mode we named. Their
    # probe found the silent fallback on `keyword`, on `auto`, AND on sending no mode at all; only
    # explicitly asking for the tail makes a tail the right answer.
    asked_for_keyword = bool(wanted_keyword) and (requested_mode or "").lower() != "tail"

    if lines is None:
        outcome = "unreadable"
    elif matched:
        # Locally confirmed. This is the only path that may be called a hit, and note that it holds
        # even when they downgraded the method: if the tail happens to contain the term, it contains
        # the term. What is refused is calling an UNCONFIRMED line a match.
        outcome = "keyword_match"
    elif asked_for_keyword and is_tail:
        outcome = "semantic_downgrade"
    elif not lines:
        outcome = "no_match"
    elif is_context:
        outcome = "time_context"
    elif is_tail:
        outcome = "tail_context"
    else:
        # Lines came back, nothing in them contains the term, and they did not say how they read.
        # Unknown provenance plus no local confirmation is not a hit.
        outcome = "no_match" if wanted_keyword else "tail_context"

    return {
        "outcome": outcome,
        "actual_method": actual,
        "requested_mode": requested_mode or "",
        "requested_keyword": wanted_keyword,
        "lines": lines,
        "literal_matches": matched,
        # Lines that came back but are NOT confirmed matches. Readable by a human as surrounding
        # context; never counted, quoted, or described as a keyword result.
        "context_lines": [] if matched else list(lines or []),
        "semantic_downgrade": outcome == "semantic_downgrade",
        # The whole point of the split: only this licenses evidence.
        "evidence_accepted": outcome == "keyword_match",
        "reported_count": reported,
        "error": error,
    }


def extract_app_names(text, structured=None, operation="log.list_apps"):
    """A `log.list_apps` response -> (names, note, error). `names is None` means fail closed.

    This list is a VERIFICATION set: a candidate app name is queried only if it appears here. So
    over-inclusion is the dangerous direction — a fabricated entry lets an unverified name through,
    the query comes back empty, and empty reads as "no problem". Hence directories only, and hence
    failing closed rather than harvesting whatever tokens the body happens to contain.

    The kind filter is enforced whenever the batch carries a kind field at all. Requiring it
    unconditionally would refuse a server that simply does not send one — that is a shape we have
    never observed, and refusing every app over it is its own silent outage.
    """
    shape = response_shape(operation)
    body = _decode(text, structured)
    if body is None:
        names = {token.strip(" \t\"',[]{}:") for token in re.split(r"[\s,]+", text or "")}
        return sorted(n for n in names if n), "plain-text listing (legacy shape)", ""
    rows = _rows(body, shape.get("entries") or ())
    if rows is None:
        return None, "", _shape_error(operation, "entries", shape.get("entries") or ())
    kind_keys = shape.get("kind") or ()
    want = shape.get("kind_value")
    name_keys = shape.get("name") or ()
    kinds = [next((str(row[k]) for k in kind_keys if isinstance(row.get(k), str)), None)
             for row in rows if isinstance(row, dict)]
    enforce = bool(want) and any(kind is not None for kind in kinds)
    names, filtered, unnamed = [], 0, 0
    for row in rows:
        if isinstance(row, str):
            if row.strip():
                names.append(row.strip())        # legacy list-of-strings, still accepted
            continue
        if not isinstance(row, dict):
            return None, "", _shape_error(
                operation, "entries", shape.get("entries") or (),
                extra=f"An entry was a {type(row).__name__}. ")
        if enforce:
            kind = next((str(row[k]) for k in kind_keys if isinstance(row.get(k), str)), None)
            if kind != want:
                filtered += 1                    # a file, or something that is not an app
                continue
        name = next((row[k] for k in name_keys if isinstance(row.get(k), str)), "")
        if name.strip():
            names.append(name.strip())
        else:
            unnamed += 1
    if unnamed and not names:
        # Entries were present and NONE carried a name we recognise. Returning [] here would say
        # "this source has no apps", which is a claim about their environment we have no basis for.
        return None, "", _shape_error(
            operation, "name", name_keys,
            extra=f"{unnamed} entry/entries were found but none carried a known name field. ")
    note = ""
    if filtered:
        note = f"{filtered} non-{want} entry/entries ignored (files are not apps)"
    elif not enforce and any(isinstance(row, dict) for row in rows):
        note = ("no entry-type field was present, so files could not be told apart from apps; "
                "every named entry was accepted")
    if unnamed:
        note = (note + "; " if note else "") + f"{unnamed} entry/entries had no readable name"
    return sorted(set(names)), note, ""
_MAX_DIMENSIONS = 10


def alarm_metric_identity(body):
    """A `get_alarm` body -> the metric to query, or (None, why).

    Read strictly from the alarm's own configuration. Nothing here may be inferred from the alarm
    NAME, the repo, or an ECS resource string: `resource != namespace` and `resource != dimensions`.
    """
    if not isinstance(body, dict):
        return None, "the get_alarm response was not a JSON object"
    namespace = body.get("Namespace")
    metric = body.get("MetricName")
    if not isinstance(namespace, str) or not namespace.strip():
        return None, "the alarm carries no Namespace, so the metric identity is unknown"
    if not isinstance(metric, str) or not metric.strip():
        return None, "the alarm carries no MetricName, so the metric identity is unknown"
    raw_dims = body.get("Dimensions")
    if raw_dims is None:
        raw_dims = []
    if not isinstance(raw_dims, list):
        return None, "the alarm's Dimensions field was not a list"
    dimensions = []
    for item in raw_dims[:_MAX_DIMENSIONS]:
        if not isinstance(item, dict):
            return None, "a Dimensions entry was not an object"
        name, value = item.get("Name"), item.get("Value")
        if not isinstance(name, str) or not isinstance(value, str):
            return None, "a Dimensions entry had no string Name/Value pair"
        dimensions.append({"Name": name, "Value": value})

    def _int(key, default):
        value = body.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    statistic = body.get("Statistic")
    return {
        "namespace": namespace,
        "metric": metric,
        "dimensions": dimensions,
        # The alarm's OWN statistic/period. Letting the model pick a default would mean querying a
        # different aggregation from the one that fired.
        "statistic": statistic if isinstance(statistic, str) and statistic.strip() else "Average",
        "period_seconds": _int("Period", 300),
        "evaluation_periods": _int("EvaluationPeriods", 1),
        "threshold": body.get("Threshold") if isinstance(body.get("Threshold"), (int, float))
                     and not isinstance(body.get("Threshold"), bool) else None,
        "comparison": body.get("ComparisonOperator")
                      if isinstance(body.get("ComparisonOperator"), str) else "",
    }, ""


def parse_metric_window(body):
    """A `get_metric_window` body -> (points, status_code, error). `points is None` = fail closed.

    `points` is a list of (datetime, float) that stays in the caller's local variables. An EMPTY
    list is a real answer — "no datapoint in exactly this window" — and must never be reported as
    "the system was fine". An unreadable shape is OUR wiring problem and must never be reported as
    either.
    """
    if not isinstance(body, dict):
        return None, "", "the metric response was not a JSON object"
    stamps, values = body.get("Timestamps"), body.get("Values")
    if not isinstance(stamps, list) or not isinstance(values, list):
        return None, "", "the metric response has no Timestamps/Values lists"
    if len(stamps) != len(values):
        return None, "", (f"the metric response returned {len(stamps)} timestamps and "
                          f"{len(values)} values — mismatched lengths, so the series cannot be "
                          f"read. This is a wiring/parser failure, not a quiet metric.")
    status = body.get("StatusCode") if isinstance(body.get("StatusCode"), str) else ""
    points = []
    for raw_stamp, raw_value in zip(stamps, values):
        if not isinstance(raw_stamp, str):
            return None, status, "a metric timestamp was not a string"
        try:
            when = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        except ValueError:
            return None, status, f"a metric timestamp could not be parsed ({len(raw_stamp)} chars)"
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            return None, status, "a metric value was not a number"
        if not math.isfinite(raw_value):
            return None, status, "a metric value was NaN or infinite"
        points.append((when, float(raw_value)))
    points.sort(key=lambda pair: pair[0])
    return points, status, ""


def summarize_points(points, threshold=None, comparison=""):
    """Datapoints -> CATEGORIES. The numbers are used here and go no further.

    Deliberately coarse. `direction` and `variability` describe the window that was queried and
    nothing else; a rising line is not a root cause, and this summary must not be phrased as one.
    """
    values = [value for _when, value in points or []]
    out = {"data_presence": "present" if values else "absent",
           "direction": "insufficient_data",
           "variability": "insufficient_data",
           "threshold_relation": "not_evaluated"}
    if len(values) >= 2:
        third = max(1, len(values) // 3)
        head = sum(values[:third]) / third
        tail = sum(values[-third:]) / third
        scale = max(abs(head), abs(tail), 1e-9)
        change = (tail - head) / scale
        out["direction"] = "rising" if change > 0.1 else "falling" if change < -0.1 else "flat"

        mean = sum(values) / len(values)
        spread = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
        if abs(mean) < 1e-9:
            out["variability"] = "high" if spread > 1e-9 else "low"
        else:
            ratio = spread / abs(mean)
            out["variability"] = "low" if ratio < 0.1 else "medium" if ratio < 0.3 else "high"

    if values and isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        op = (comparison or "").lower()
        if "greater" in op:
            breaching, side, other = [v > threshold for v in values], "above", "below"
        elif "less" in op:
            breaching, side, other = [v < threshold for v in values], "below", "above"
        else:
            breaching = side = other = None
        if breaching is not None:
            out["threshold_relation"] = (side if all(breaching)
                                         else other if not any(breaching) else "crossing")
    return out


_SHAPE_MAX_DEPTH = 6
_SHAPE_MAX_KEYS = 24


def describe_shape(node, _depth=0):
    """A response body -> its STRUCTURE only: field names, types, lengths. Never a value.

    This exists so "what does your tool actually return?" can be answered from inside the intranet
    without anyone reading a production response or pasting one out. Three rounds of defects came
    from me guessing at their shapes; this is how the guessing stops without a log line travelling.

    Values never appear — a string becomes `str(len=42)`. Field NAMES do appear, because they are
    the whole point, but they are redacted too: a body keyed by account number would otherwise leak
    through the one field this function has to print.
    """
    if _depth >= _SHAPE_MAX_DEPTH:
        return "...(depth limit)"
    if isinstance(node, dict):
        keys = list(node)[:_SHAPE_MAX_KEYS]
        out = {redact(str(key)): describe_shape(node[key], _depth + 1) for key in keys}
        if len(node) > len(keys):
            out["...(%d more keys)" % (len(node) - len(keys))] = ""
        return out
    if isinstance(node, list):
        if not node:
            return "list[0]"
        kinds = sorted({type(item).__name__ for item in node})
        # Only the FIRST element is described. A heterogeneous list is worth knowing about, so the
        # types are all named even though only one is expanded.
        return {"list[%d] of %s" % (len(node), "|".join(kinds)): describe_shape(node[0], _depth + 1)}
    if isinstance(node, str):
        return "str(len=%d)" % len(node)
    if isinstance(node, bool):
        return "bool"
    return type(node).__name__


def describe_response(out, operation):
    """One MCP result -> a values-free report of its shape AND what our parsers made of it.

    The single thing to paste back from the box when a parse fails: it says what they sent, what we
    looked for, and where the two disagree, without any production text in it.
    """
    text = (out or {}).get("text") or ""
    structured = (out or {}).get("structured")
    body = _decode(text, structured)
    report = {
        "operation": operation,
        "outcome": _tool_outcome(out)[0],
        "carried_structured_content": isinstance(structured, (dict, list)),
        "body_is_json": body is not None,
        "shape": describe_shape(body) if body is not None else "not JSON (plain text)",
        "text_chars": len(text),
        "declared_shape": {k: list(v) if isinstance(v, tuple) else v
                           for k, v in response_shape(operation).items()},
    }
    if operation == "log.read":
        lines, reported, error = extract_log_lines(text, structured, operation)
        report["parsed"] = {"lines": None if lines is None else len(lines),
                            "server_reported_count": reported, "error": error}
    elif operation == "log.list_apps":
        names, note, error = extract_app_names(text, structured, operation)
        report["parsed"] = {"apps": None if names is None else len(names),
                            # Names are app/service identifiers, not customer data, and seeing a few
                            # is how you tell a real listing from JSON keys. Still exit-gated below.
                            "sample": (names or [])[:5], "note": note, "error": error}
    elif operation == "log.search_files":
        picked = select_log_files(text, structured=structured)
        report["parsed"] = {"files": len(picked), "sample": picked[:5]}
    cleaned, _check = sanitize_packet(report)
    return cleaned
_MAX_FILES_PER_SOURCE = int(os.environ.get("SDLC_INCIDENT_MAX_FILES", "2"))
# Real names confirmed in RUNBOOK-55. Used only to RANK candidates that the server returned — never
# to invent a file name, which is what hard-coding `otx_trace.log` amounted to.
_PREFERRED_LOG_FILES = ("otx_trace.log", "exception.log", "sftp.log")
_FILE_TOKEN = re.compile(r"[\w./\\-]*\.log(?:[._-]?\d{6,8})?")


def select_log_files(text, alert_date="", limit=None, structured=None):
    """Parse a `log.search_files` response into a bounded, ranked list of real file names.

    Returns [] when nothing parses. That is the whole point: an empty candidate list must end in
    "we could not identify a log file", never in a guessed name. Hard-coding `otx_trace.log` was
    exactly that guess, and it also mislabelled evidence when the real read was something else.

    Same rule as the other two parsers: a JSON body is read structurally and the regex fallback is
    NOT reached from it, so an unrecognised shape yields nothing instead of arbitrary tokens.
    """
    limit = _MAX_FILES_PER_SOURCE if limit is None else limit
    shape = response_shape("log.search_files")
    body = _decode(text, structured)
    names = []
    if body is None:
        names = _FILE_TOKEN.findall(text or "")
    else:
        exclude = {str(v).lower() for v in shape.get("kind_exclude") or ()}
        for row in _rows(body, shape.get("entries") or ()) or ():
            if isinstance(row, str):
                names.append(row)
            elif isinstance(row, dict):
                kind = next((str(row[k]).lower() for k in shape.get("kind") or ()
                             if isinstance(row.get(k), str)), "")
                if kind and kind in exclude:
                    continue                    # a directory is not a file to read
                value = next((row[k] for k in shape.get("name") or ()
                              if isinstance(row.get(k), str)), "")
                if value:
                    names.append(value)

    cleaned, seen = [], set()
    for name in names:
        name = (name or "").strip().strip("\"',")
        if not name or ".log" not in name.lower() or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)

    def _rank(name):
        base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        # Same-day files first when the alert date is known, then the known log types, then shortest
        # (a bare `otx_trace.log` beats a rotated `otx_trace.log.20260701`).
        return (0 if alert_date and alert_date.replace("-", "") in base else 1,
                next((i for i, known in enumerate(_PREFERRED_LOG_FILES) if known in base), 99),
                len(base))

    return sorted(cleaned, key=_rank)[:max(1, limit)]


def _tool_outcome(out):
    """An MCP result -> ("error"|"empty"|"hit", text). Four outcomes, not two.

    A call can (1) fail in transport — raised, never reaches here; (2) run and report failure
    (`ok: False`); (3) run and match nothing; (4) run and return content. Reading `text`
    unconditionally collapses 2 into 4, which is the single worst bug this feature can have: the
    tool's own error message ("unknown source hkl") is non-empty, so it would be wrapped up and
    presented as log evidence. A failed call must never be able to look like a finding.
    """
    if not isinstance(out, dict):
        return "error", "malformed MCP result"
    if not out.get("ok") or out.get("tool_reported_error"):
        return "error", (out.get("text") or out.get("error") or "the tool reported failure")
    text = out.get("text") or ""
    return ("hit", text) if text.strip() else ("empty", "")


# Portal's forward record is a DELIVERY record, so a hit is the most PII-dense thing this whole
# module can touch: recipient, payload, template, message body. None of it is needed to answer "did
# this message get delivered and if not, what kind of failure" — so the packet carries CATEGORIES
# only and the raw JSON never leaves this function.
_PORTAL_STATUSES = ("delivered", "failed", "pending", "unknown")
_PORTAL_FAILURE_KINDS = ("policy", "provider", "template", "routing", "unknown")
# Field names we are willing to READ out of their response. Anything not here is not looked at, so a
# response that grows a `recipient` or `messageBody` field cannot leak by accident.
_PORTAL_STATUS_KEYS = ("status", "deliverystatus", "delivery_status", "state", "result")
_PORTAL_REASON_KEYS = ("failurereason", "failure_reason", "reason", "errorcategory",
                       "error_category", "rejectedreason", "rejected_reason")


def _portal_status(value):
    """Their status text -> one of `_PORTAL_STATUSES`. Unknown maps to `unknown`, never to a guess."""
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    for status in ("delivered", "failed", "pending"):
        if status in text:
            return status
    if text in ("success", "ok", "sent", "complete", "completed"):
        return "delivered"
    if text in ("reject", "rejected", "error", "bounce", "bounced"):
        return "failed"
    return "unknown"


def _portal_failure_kind(value):
    """Their reason text -> a coarse category. Deliberately lossy: a reason string can carry a
    template name or a customer identifier, and the category is what an incident answer needs."""
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    for kind, markers in (("policy", ("consent", "optout", "opt-out", "blacklist", "policy",
                                      "suppress")),
                          ("provider", ("vendor", "provider", "gateway", "carrier", "smsc",
                                        "mmsc", "upstream", "timeout", "connection")),
                          ("template", ("template", "content", "render", "payload", "format")),
                          ("routing", ("route", "routing", "router", "topic", "channel"))):
        if any(marker in text for marker in markers):
            return kind
    return "unknown"


def _parse_log_groups(body):
    """Log-group names from their response, or None when the shape is not recognised.

    None is a PARSER GAP, not an empty result — the caller must report it as our wiring problem.
    """
    if body is None:
        return None
    rows = body if isinstance(body, list) else _rows(body, ("logGroups", "log_groups", "groups",
                                                            "items", "results"))
    if rows is None:
        return None
    names = []
    for row in rows:
        if isinstance(row, str) and row.strip():
            names.append(row.strip())
        elif isinstance(row, dict):
            for key in ("logGroupName", "log_group_name", "name", "logGroup"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
                    break
    # `[]` when the container was RECOGNISED and empty; `None` only when it was not recognised. An
    # earlier `names or None` here collapsed those two — which is the exact confusion this module
    # exists to prevent, reintroduced one level down: "this resource has no log groups" would have
    # been reported as "our parser is broken".
    return names


def _parse_cloudwatch_log_lines(body):
    """Message strings from a Logs Insights result, or None when the shape is not recognised.

    Reads ONLY explicit `@message`/`message` fields. Never `str(body).splitlines()` — that turns an
    error envelope, or a field nobody scoped, into "log lines" (the 2026-07-30 defect class).
    """
    if body is None:
        return None
    rows = body if isinstance(body, list) else _rows(body, ("results", "events", "records",
                                                            "messages", "items"))
    if rows is None:
        return None
    lines = []
    for row in rows:
        if isinstance(row, str):
            if row.strip():
                lines.append(row)
            continue
        if isinstance(row, list):
            # Insights returns [{"field": "@message", "value": "..."}, ...] per row.
            for cell in row:
                if isinstance(cell, dict) and str(cell.get("field", "")).lower() in (
                        "@message", "message"):
                    value = cell.get("value")
                    if isinstance(value, str) and value.strip():
                        lines.append(value)
            continue
        if isinstance(row, dict):
            for key in ("@message", "message", "Message"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(value)
                    break
    # Same rule as `_parse_log_groups`: recognised-and-empty is `[]`, unrecognised is `None`.
    return lines


# Tag keys worth reporting the PRESENCE of. Values are never kept: a tag value carries person
# names, emails, team abbreviations and internal system ids, and none of that is needed to answer
# "is this resource owned/labelled". Their live probe: an ECS service sample had 10 keys including
# `owner`, and no `application` or `support group`.
_TAG_KEYS_OF_INTEREST = {
    "owner": "owner_tag_present",
    "application": "application_tag_present",
    "support group": "support_group_tag_present",
    "supportgroup": "support_group_tag_present",
    "environment": "environment_tag_present",
}


def _tag_keys(body):
    """Tag KEYS from their response, or None when the shape is not recognised.

    Confirmed live shape (2026-08-03): `{resourceArn, tags{key: value}, rawTags[{Key, Value}]}`.
    `tags` is preferred; `rawTags` is the fallback. `resourceArn` is used only to confirm we got the
    scope we asked for and is then dropped — it never enters the packet.
    """
    if not isinstance(body, dict):
        return None
    tags = body.get("tags")
    if isinstance(tags, dict):
        return [str(key) for key in tags]
    raw = body.get("rawTags")
    if isinstance(raw, list):
        return [str(item.get("Key")) for item in raw
                if isinstance(item, dict) and item.get("Key") is not None]
    return None
