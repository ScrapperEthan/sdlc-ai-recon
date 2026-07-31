"""The incident investigator: the only thing in this system that touches raw production logs.

It exists to be a *wall*, not a feature. The main agent's conversation is persisted to
`webapp_data/chat_sessions.json`; raw production log text must never land there, and "we were
careful" is not a control. So the split is structural: this module reads logs into local variables,
redacts and aggregates them, and returns a **structured evidence packet**. The raw text is never
returned, never yielded, never logged, and is unreachable once `investigate()` returns.

Three defences, deliberately redundant, because the expensive failure here is silent:

1. **Redact at extraction.** Every line is passed through `redact()` before it is put anywhere that
   could end up in the packet.
2. **Verify at the exit.** `sanitize_packet()` re-scans the finished packet and strips anything that
   still matches a PII pattern, recording that it had to. If defence 1 is ever bypassed by a new
   code path, this still holds, and the `sanitized_at_exit` counter makes the bypass visible instead
   of silent.
3. **Bound what leaves at all.** Excerpt count and length are capped, so even a redaction miss is
   limited in blast radius rather than shipping a whole log file.

## production vs dev, and why every item carries a label

Two different data sources with opposite handling rules meet in one answer:

* **LogDream's sources and CloudWatch are PRODUCTION** (owner confirmed 2026-07-29: both LogDream
  sources are production, holding different logs — and each has its OWN app list, so an app present
  on one is not necessarily present on the other).
* **The use-case route snapshot is dev/SCT**, not production — a use case absent there is not
  evidence it is absent in production.

Mixing those two silently is how "this is what production does" gets asserted from a dev export. So
every evidence item states its `environment`, and the packet carries
`contains_production_data`, which is what a caller keys the storage rules off.

## The query plan is the part a generic AIOps cannot write

Alert text -> which app, which files, which window, **which keywords**. The keywords come from our
own graph: the topics that repo actually produces or consumes, the use cases on that delivery path,
its channel and carrier, and the exception classes that genuinely exist in its source. Nobody
without the code graph can produce that list, and it is the difference between "search the logs for
ERROR" and "search for the four exceptions this service can actually throw".

Everything in the plan fails closed:

* An app name is only used if it appears in the server's OWN `log.list_apps` output. RUNBOOK-55
  measured repo->app naming at 0% identical and ~36% by rule, so a rule-derived name is a
  *candidate*, never an answer.
* A time window is never invented. Three timezones coexist (CloudWatch UTC / LogDream
  Asia/Hong_Kong / servers GMT); a helpfully-defaulted window returns nothing and reads as "no
  anomaly", which is the worst possible failure for this feature. Without one the plan is NOT
  runnable and zero calls are made — see `plan()`.
* A structured response is read structurally. Their bodies are JSON; reading JSON as text is how a
  2-line response was reported as 11 lines and how `entries`/`entry_type` became "app names"
  (intranet, 2026-07-31). See the response-shape section below.
"""
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta
from datetime import timezone as _utc_tz          # `timezone` is a parameter name all over this file

from retriever import code as rcode, incident, messages as msg, repo_tags
from . import config, incident_raw_store, mcp_client, mcp_registry

# LogDream's sources are BOTH production, holding different logs, so both are queried and every piece
# of evidence says which one it came from (owner, 2026-07-29).
#
# These are ENVIRONMENT VOCABULARY, so the intranet's config is authoritative and this is only the
# fallback. Hard-coding them here was a mistake of exactly the kind RUNBOOK-49/50/51 were three
# separate fixes for: the literal was `hk1` (digit one) where the server accepts `hkl` (letter L),
# which the box reported in RUNBOOK-60 and which silently loses half the log coverage — every query
# against the bad name comes back rejected, and a rejection that is read as "no lines" reads as
# "no problem".
DEFAULT_LOG_SOURCES = ("hkl", "hkp3")


def log_sources():
    """Sources to query, from `servers.logdream.sources` in the intranet config; else the default.

    A source with `query_by_default: false` is skipped. Whatever this returns is still validated
    against the live server before anything is searched (each source's app listing must succeed), so
    a wrong name produces a loud, specific refusal naming that source rather than half the queries
    quietly finding nothing.
    """
    declared = (mcp_registry.servers().get("logdream") or {}).get("sources")
    if isinstance(declared, dict):
        names = [str(name).strip() for name, spec in declared.items()
                 if not str(name).startswith("_") and str(name).strip()
                 and (not isinstance(spec, dict) or spec.get("query_by_default", True))]
        if names:
            return tuple(names)
    return DEFAULT_LOG_SOURCES


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


# ---- CloudWatch: the metric half of an incident ------------------------------------------------
# The log branch answers "what did the service say". This one answers "what was the shape of the
# thing that fired the alarm". They are kept apart on purpose: separate accounting, separate
# refusals, and either can run when the other cannot (intranet handoff, 2026-07-31 §7).
#
# The rules that differ from the log branch, and why:
#
# * **Metric identity is read from the ALARM, never derived.** `resource` is not a namespace and a
#   repo name is not a dimension. `get_alarm` returns Namespace/MetricName/Dimensions/Statistic/
#   Period; anything missing means we stop, because a guessed identity silently returns a DIFFERENT
#   service's numbers.
# * **This is the one place a moment IS converted.** CloudWatch wants UTC. The log branch never
#   converts (it sends the stamp and the zone separately); mixing the two rules up puts the metric
#   window eight hours from the incident.
# * **Only categories leave.** The datapoints are used in local variables to decide rising/flat/
#   high-variability and are then dropped. No value, no average, no min/max, no delta reaches the
#   packet — the session is persisted, and a metric series is production data.

# The window is a QUERY STRATEGY, not a fact about the alarm, so it is bounded, configurable, and
# stated in the evidence.
_METRIC_MINUTES_BEFORE = int(os.environ.get("SDLC_INCIDENT_METRIC_MINUTES_BEFORE", "15"))
_METRIC_MINUTES_AFTER = int(os.environ.get("SDLC_INCIDENT_METRIC_MINUTES_AFTER", "15"))
# Hard ceiling on either side. `Period x EvaluationPeriods` widens the "before" side so the window
# covers what the alarm actually evaluated, but a pathological alarm config must not be able to ask
# for a week of datapoints.
_METRIC_MAX_MINUTES = int(os.environ.get("SDLC_INCIDENT_METRIC_MAX_MINUTES", "180"))
_METRIC_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MAX_DIMENSIONS = 10

# Zones with no DST, where a fixed offset is exact rather than an approximation. Hong Kong has not
# observed DST since 1979, which covers every incident anyone will investigate here. This exists
# because a bare Windows box has no tz database — `zoneinfo` needs the `tzdata` package, which an
# air-gapped install does not have — and "convert with whatever offset" is not an option.
_FIXED_OFFSET_ZONES = {
    "ASIA/HONG_KONG": 8 * 3600, "HONGKONG": 8 * 3600,
    "UTC": 0, "GMT": 0, "ETC/UTC": 0, "ETC/GMT": 0, "Z": 0,
}


def to_utc(stamp, zone):
    """`2026-07-30 03:15:00` + `Asia/Hong_Kong` -> (aware UTC datetime, how), or (None, why).

    `zoneinfo` first, so any zone with DST is handled correctly where the tz database exists; the
    fixed-offset table backs it up for the no-DST zones when it does not. An unknown zone returns
    None and the metric call is skipped: a window converted with the wrong offset comes back full
    of datapoints from the wrong hour, which is worse than no window at all.
    """
    try:
        naive = datetime.strptime(stamp, ALERT_TIME_FORMAT)
    except (TypeError, ValueError):
        return None, f"the alert time {stamp!r} is not in {ALERT_TIME_FORMAT}"
    key = (zone or "").strip()
    if not key:
        return None, "no timezone, so the alert time cannot be placed on the UTC timeline"
    try:
        from zoneinfo import ZoneInfo
        return naive.replace(tzinfo=ZoneInfo(key)).astimezone(_utc_tz.utc), "zoneinfo"
    except Exception:                     # noqa: BLE001 -- absent tzdata, unknown key: same handling
        pass
    offset = _FIXED_OFFSET_ZONES.get(key.upper())
    if offset is None:
        return None, (f"timezone {key!r} could not be resolved (no tz database on this host and it "
                      f"is not one of the fixed-offset zones). The metric window was NOT built — a "
                      f"wrongly converted window returns the wrong hour's datapoints.")
    shifted = naive.replace(tzinfo=_utc_tz(timedelta(seconds=offset))).astimezone(_utc_tz.utc)
    return shifted, f"fixed offset table ({key}, no DST)"


def metric_window_bounds(alert_utc, period_seconds=300, evaluation_periods=1):
    """The UTC window to query around an alert. Bounded and stated, never derived from `now()`."""
    evaluated = max(1, int(period_seconds or 300)) * max(1, int(evaluation_periods or 1))
    before = min(_METRIC_MAX_MINUTES,
                 max(_METRIC_MINUTES_BEFORE, math.ceil(evaluated / 60)))
    after = min(_METRIC_MAX_MINUTES, max(0, _METRIC_MINUTES_AFTER))
    start = alert_utc - timedelta(minutes=before)
    end = alert_utc + timedelta(minutes=after)
    fmt = ((mcp_registry.operations().get("aws.metric_window") or {}).get("request") or {}).get(
        "time_format")
    fmt = fmt if isinstance(fmt, str) and fmt.strip() else _METRIC_TIME_FORMAT
    return {
        "start_utc": start.strftime(fmt),
        "end_utc": end.strftime(fmt),
        "basis": "the alert time plus its explicit timezone, converted to UTC",
        "policy": (f"{before} minute(s) before and {after} after the alert. This is OUR query "
                   f"strategy, not a window CloudWatch defines; the 'before' side is widened to "
                   f"cover Period x EvaluationPeriods ({evaluated}s) and capped at "
                   f"{_METRIC_MAX_MINUTES} minutes."),
    }


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


_MAX_EXCERPTS = 5
_MAX_EXCERPT_CHARS = 300
_MAX_KEYWORDS = 8
# Hard ceiling on log reads per investigation. Keywords x sources multiplies fast — 8 keywords over
# two production sources is 16 calls, and RUNBOOK-55 clocked a single MCP call at 26.4s, so an
# unbounded sweep is a seven-minute answer. Which keywords were actually spent is always reported,
# because a nil result is only meaningful for the queries that ran.
_MAX_LOG_QUERIES = int(os.environ.get("SDLC_INCIDENT_MAX_LOG_QUERIES", "6"))


# ---- redaction ------------------------------------------------------------------------------
# Each pattern keeps a short stable digest of what it replaced, so the same account appearing on
# five lines is still recognisably the same account without the value ever being exposed. A plain
# `***` would destroy the correlation that makes a log excerpt worth reading at all.
_PII = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # +852 / 8-digit HK mobiles / any 7+ run that is punctuated like a phone number.
    ("phone", re.compile(r"(?<!\d)(?:\+?852[-\s]?)?\d{4}[-\s]?\d{4}(?!\d)")),
    # Card/account-like: 12-19 digits, optionally grouped. Anchored to end ON a digit so it does not
    # swallow the following separator and glue two words together in the excerpt.
    ("account", re.compile(r"(?<!\d)\d(?:[ -]?\d){11,18}(?!\d)")),
    # MDC tracking ids and similar long opaque handles: they are customer-linkable, but dropping
    # them entirely would make it impossible to see that 40 lines concern ONE message. The trailing
    # group is greedy so the WHOLE handle is replaced — matching only its first half would leave a
    # fragment behind and defeat the point. Each group needs 4+ chars, which keeps `2026-07-30` and
    # `AWS-HK-SNS` out of scope.
    ("tracking", re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")),
    ("hkid", re.compile(r"\b[A-Z]{1,2}\d{6}\(?[0-9A]\)?\b")),
)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:6]


def redact(text, counts=None):
    """Text with every PII-shaped run replaced by `<kind:digest>`. Idempotent."""
    if not text:
        return ""
    out = text
    for kind, pattern in _PII:
        def _sub(match, kind=kind):
            if counts is not None:
                counts[kind] = counts.get(kind, 0) + 1
            return f"<{kind}:{_digest(match.group(0))}>"
        out = pattern.sub(_sub, out)
    return out


def _residual_pii(text):
    """Which patterns still match — the exit check. Our own markers must not re-trigger it."""
    stripped = re.sub(r"<\w+:[0-9a-f]{6}>", " ", text or "")
    return sorted({kind for kind, pattern in _PII if pattern.search(stripped)})


# Machine-generated identifiers, exempt from the exit scan. `uuid4().hex` frequently contains eight
# consecutive digits, which the account/phone patterns match — so without this the gate ate roughly
# one in eight `raw_ref`s, silently breaking click-through for that evidence item AND inflating
# `sanitized_at_exit`, the counter whose whole job is to signal a REAL upstream redaction bug. A
# false positive in a leak detector is not harmless; it trains you to ignore it.
#
# Safe because these values never come from log content: `raw_ref` is set only from
# `incident_raw_store.put()`, and app/source/file names come from the server's own listings by way of
# our selection. A dated log file (`otx_trace.log.20260701`) is eight consecutive digits and matches
# the phone pattern, so without this the gate mangles the one field an operator needs to go look at
# the file themselves. Do not add a key here that could carry log-derived text.
_IDENTIFIER_KEYS = frozenset({"raw_ref", "file", "app", "source"})


def sanitize_packet(node, report=None):
    """Walk a finished packet and blank any string that still looks like PII.

    Defence 2. Reaching this should mean a bug upstream, so it counts what it had to do rather than
    fixing it quietly: a silent save is indistinguishable from correct behaviour.
    """
    report = report if report is not None else {"sanitized_at_exit": 0, "kinds": []}
    if isinstance(node, dict):
        return {key: (value if key in _IDENTIFIER_KEYS and isinstance(value, str)
                      else sanitize_packet(value, report)[0])
                for key, value in node.items()}, report
    if isinstance(node, list):
        cleaned = []
        for item in node:
            value, _ = sanitize_packet(item, report)
            cleaned.append(value)
        return cleaned, report
    if isinstance(node, str):
        kinds = _residual_pii(node)
        if kinds:
            report["sanitized_at_exit"] += 1
            report["kinds"] = sorted(set(report["kinds"]) | set(kinds))
            # Mask the matching span rather than discarding the whole string. Blanking it protected
            # the data but destroyed the message around it — and most of these strings are prose WE
            # composed ("the query budget ran out, so these were never tried: ..."), where losing the
            # sentence costs an operator the reason and gains nothing. `redact` is the same masking
            # used upstream, so a genuine leak is still neutralised here.
            return redact(node), report
        return node, report
    return node, report


# ---- the query plan -------------------------------------------------------------------------

def app_candidates(repo):
    """Repo id -> candidate LogDream app names, with how each was derived.

    RUNBOOK-55: 0% are identical, ~36% resolve by rule. So these are candidates to be checked
    against the server's own app list, never answers. An intranet-owned mapping wins when present;
    absent, the built-in rule applies — the box owns that file and cannot push it here.
    """
    repo = (repo or "").strip()
    if not repo:
        return []
    mapped = _app_map().get(repo.lower())
    if mapped:
        return [{"app": mapped, "how": "config/logdream_apps.json (intranet-owned mapping)",
                 "confidence": "confirmed"}]
    stem = repo
    for prefix in ("mc-hk-hase-", "amet-mdc-", "mc-hk-"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    for suffix in ("-job", "-api", "-service", "-svc"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = [part for part in stem.split("-") if part]
    if not parts:
        return []
    camel = parts[0] + "".join(word.capitalize() for word in parts[1:])
    out = [{"app": camel, "how": "rule: drop org prefix + role suffix, kebab->camel",
            "confidence": "candidate"}]
    if stem != camel:
        out.append({"app": stem, "how": "rule: drop org prefix + role suffix only",
                    "confidence": "candidate"})
    return out


# Both spellings are accepted. The intranet's own gap analysis calls this file
# `config/logdream_app_map.json` while this module first shipped reading `logdream_apps.json`, and
# config/ is intranet-owned on a box that cannot push — so a name disagreement would surface as the
# mapping silently having no effect, which is the worst possible failure mode for a knob. Accepting
# either costs one line; coordinating a rename across an air gap does not.
_APP_MAP_FILES = ("logdream_app_map.json", "logdream_apps.json")
# Likewise for the key: `repo_to_app` is what this module documented, but a hand-written file may
# reasonably be a flat {repo: app} object.
_APP_MAP_KEYS = ("repo_to_app", "repo_to_logdream_app", "mapping")


def _app_map():
    """{repo -> app}. Intranet knob, read if present. Absent is normal, not an error."""
    import json
    for name in _APP_MAP_FILES:
        try:
            with open(os.path.join(os.getcwd(), "config", name), encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        mapping = next((data[key] for key in _APP_MAP_KEYS
                        if isinstance(data.get(key), dict)), None)
        if mapping is None:
            # A flat {repo: app} file, minus any documentation keys.
            mapping = {k: v for k, v in data.items()
                       if not str(k).startswith("_") and isinstance(v, str)}
        cleaned = {str(k).strip().lower(): str(v).strip() for k, v in mapping.items()
                   if not str(k).startswith("_") and str(v).strip()}
        if cleaned:
            return cleaned
    return {}


_EXCEPTION_CLASS = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Exception|Error|Throwable))\b")


def exception_classes(repo, limit=6):
    """Exception types that genuinely appear in this repo's source.

    This is the half of the keyword list a generic AIOps cannot produce: not "search for ERROR", but
    the specific types this service is actually able to throw. Empty when the mirror is unavailable —
    a keyword we cannot substantiate is simply not offered.
    """
    try:
        # Returns a list of 'path:line:text' strings, one per match.
        hits = rcode.search_code(r"throw new \w+(Exception|Error)", glob="*.java",
                                 max_results=60, repos=[repo])
    except Exception:                                    # noqa: BLE001 — mirror absent is normal
        return []
    found = {}
    for line in hits if isinstance(hits, list) else []:
        for match in _EXCEPTION_CLASS.finditer(str(line)):
            name = match.group(1)
            found[name] = found.get(name, 0) + 1
    return [name for name, _count in sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


# The wire format for `alert_time`. The real tool REJECTED `2026-07-30 03:15 HKT` and wants
# `alert_time=2026-07-30 03:15:00` with `timezone=Asia/Hong_Kong` alongside it (intranet,
# 2026-07-31). Passing the alert's own words through was meant to avoid converting the moment —
# and it still does; this is a reformat, not a conversion. Configurable for the same reason
# everything else about their side is: `operations['log.read'].request.alert_time_format`.
ALERT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def alert_time_format(operation="log.read"):
    declared = ((mcp_registry.operations().get(operation) or {}).get("request") or {})
    fmt = declared.get("alert_time_format")
    return fmt if isinstance(fmt, str) and fmt.strip() else ALERT_TIME_FORMAT


def _format_alert_time(normalized, operation="log.read"):
    fmt = alert_time_format(operation)
    if fmt == ALERT_TIME_FORMAT:
        return normalized                     # already that shape; no parse, no drift
    try:
        return datetime.strptime(normalized, ALERT_TIME_FORMAT).strftime(fmt)
    except ValueError:
        return normalized                     # a bad format string must not lose the stamp


def plan(alert_text, repos=None, timezone=None, keywords=None, sources=None, alert_time=None,
         alarm_name=None):
    """Alert text -> a read-only query plan. Opens no sockets; fully testable offline.

    `keywords` and `sources` are the drill-down path: a follow-up question ("search for
    ConnectException instead", "only hkp3") re-runs with them instead of the derived list, so
    narrowing does not mean starting over. Caller-supplied keywords are marked as such, because the
    provenance of a keyword is what separates our query plan from "grep for ERROR".
    """
    parsed = incident.parse_alert(alert_text, repos=repos)
    out = {
        "ok": False,
        "parsed": parsed,
        "targets": [],
        "keywords": [],
        "window": None,
        "sources": [str(s).strip() for s in (sources or ()) if str(s).strip()]
                   or list(log_sources()),
        "log_files": ["otx_trace.log", "exception.log"],
        "refusals": [],
        # The CloudWatch half. Kept separate so either branch can run when the other cannot: an
        # unresolvable alarm name must not block a log investigation that is ready to go, and an
        # unidentifiable repo must not block a metric lookup that only needs the alarm name.
        "cloudwatch": {"runnable": False, "alarm_name": "", "alarm_name_source": "",
                       "refusals": []},
    }
    if out["sources"] != list(log_sources()):
        # Built from the configured source names, never a literal: the last time these were spelled
        # out by hand the text said `hk1` where the server accepts `hkl`.
        out["sources_note"] = (
            "sources narrowed by the caller. %s are ALL production with different content, so a "
            "single-source result covers less than the default — say which one was searched."
            % " and ".join(log_sources()))
    identified = bool(parsed["identified"])
    if not identified:
        out["refusals"].append(
            "no repo and no known use-case id could be read from this alert, so there is nothing to "
            "query. Ask for the service name or the use-case id; do not guess an app.")

    seen_keywords = {}

    def _add_keyword(term, why):
        term = (term or "").strip()
        if term and term.lower() not in seen_keywords:
            seen_keywords[term.lower()] = {"term": term, "why": why}

    for entry in parsed["repos"] if identified else []:
        repo = entry["repo"]
        target = {"repo": repo, "app_candidates": app_candidates(repo),
                  "app_resolved": "", "app_note": ""}
        if not target["app_candidates"]:
            target["app_note"] = ("no LogDream app name could be derived from this repo id; "
                                  "an intranet mapping is needed (config/logdream_apps.json)")
        out["targets"].append(target)

        for topic in (msg.routes_for_repo(repo) or [])[:4]:
            name = topic.get("destination") if isinstance(topic, dict) else topic
            _add_keyword(name, f"topic on {repo}'s delivery path")
        for channel in repo_tags.channels_for_repo(repo) or []:
            _add_keyword(channel, f"channel served by {repo}")
        for name in exception_classes(repo):
            _add_keyword(name, f"exception class present in {repo}'s source")

    for item in parsed["use_cases"] if identified else []:
        _add_keyword(item.get("use_case"), "use-case id named in the alert text")

    if identified and parsed.get("metric"):
        _add_keyword(parsed["metric"], "metric named in the alert")

    # Filtered BEFORE the branch: an all-whitespace override would otherwise leave zero keywords,
    # and zero keywords means zero queries — an investigation that searched nothing while looking
    # like it ran. Blank in, derived list out.
    supplied = [str(term).strip() for term in (keywords or []) if str(term).strip()]
    if supplied:
        # A drill-down replaces the derived list rather than adding to it: the point of "search for
        # X instead" is to spend the query budget on X, not to bury it behind six derived terms.
        out["keywords"] = [{"term": term,
                            "why": "supplied by the user for this follow-up (not derived from the "
                                   "code graph — say so when reporting)"}
                           for term in supplied][:_MAX_KEYWORDS]
        out["keywords_note"] = ("derived keywords were REPLACED by the caller's. The graph-derived "
                                "list is what makes a nil result meaningful, so a nil result here "
                                "only speaks to the terms the user asked for.")
    else:
        out["keywords"] = list(seen_keywords.values())[:_MAX_KEYWORDS]

    # The window. Never defaulted and never CONVERTED — see the module docstring on the three
    # coexisting timezones. TWO independent halves are required, not one: a full date+time stamp
    # and a zone, because the real tool takes them as separate parameters. They are resolved
    # separately so a refusal can name exactly which half is missing — "which timezone?" and
    # "which day?" are different questions, and asking the wrong one wastes a round trip mid-incident.
    times = [t for t in parsed.get("times") or [] if isinstance(t, dict)]
    dated = [t for t in times if t.get("normalized")]
    caller_stamp = incident.normalize_stamp(alert_time) if alert_time else ""

    stamp, shown, stamp_source = "", "", ""
    if dated:
        stamp, shown, stamp_source = dated[0]["normalized"], dated[0].get("text") or "", \
            "explicit in the alert text"
    elif caller_stamp:
        stamp, shown, stamp_source = caller_stamp, alert_time, "caller-supplied"

    zone, zone_source = "", ""
    from_alert = next((t["timezone"] for t in times if t.get("timezone")), "")
    if from_alert:
        # An explicit zone in the alert beats a caller-supplied one: the alert is the evidence.
        zone, zone_source = from_alert, "explicit in the alert text"
    elif timezone:
        zone, zone_source = timezone, "caller-supplied (the alert's own time was ambiguous)"

    if stamp and zone:
        out["window"] = {
            "at": shown,
            # What actually goes on the wire. The zone travels as its own parameter beside it.
            "alert_time": _format_alert_time(stamp),
            "timezone": zone,
            "source": ("explicit in the alert text"
                       if stamp_source == zone_source == "explicit in the alert text"
                       else f"time {stamp_source or 'missing'}; timezone {zone_source or 'missing'}"),
            "note": ("the stamp and the zone travel as SEPARATE parameters; the stamp was only "
                     "REFORMATTED, never converted — 03:15 in the alert is still 03:15 here."),
        }
    else:
        missing = []
        if not stamp:
            missing.append(
                "a DATE — the alert carries %s, and the read tool needs a full `alert_time`, which "
                "cannot be built without knowing which day" % (
                    "only a clock time like 03:15" if times else "no timestamp at all"))
        if not zone:
            missing.append(
                "a TIMEZONE — CloudWatch is UTC, LogDream defaults to Asia/Hong_Kong and the "
                "servers are GMT, so the same clock time is three moments 8 hours apart")
        out["refusals"].append(
            "BLOCKING: no time window could be built, so NOTHING was queried. Missing: "
            + "; and ".join(missing) + ". Ask the user, then pass `alert_time` (e.g. "
            "'2026-07-30 03:15:00') and/or `timezone` explicitly. Do not guess either half: a "
            "wrong window returns nothing, and nothing reads as 'no anomaly'.")

    # ---- the CloudWatch half: an alarm name, and the same window converted to UTC ---------------
    # Priority is fixed and narrowing: what the user said, then what the alert literally contains.
    # There is no third guess — a wrong alarm name does not error, it returns a DIFFERENT service's
    # metrics under this incident's heading.
    cw = out["cloudwatch"]
    supplied_alarm = incident.valid_alarm_name(alarm_name) if alarm_name else ""
    if alarm_name and not supplied_alarm:
        cw["refusals"].append(
            "the supplied alarm_name is not usable (empty, multi-line, or over "
            f"{incident.MAX_ALARM_NAME} chars), so no CloudWatch call was made.")
    if supplied_alarm:
        cw["alarm_name"], cw["alarm_name_source"] = supplied_alarm, "supplied by the caller"
    else:
        extracted = incident.extract_alarm_name(alert_text)
        if extracted:
            cw["alarm_name"] = extracted
            cw["alarm_name_source"] = "extracted from the alert text (single `AlarmName:` line)"
        else:
            cw["refusals"].append(
                "no single CloudWatch alarm name could be read from this alert, so the metric "
                "branch was skipped. Pass `alarm_name` if you know it. The name is NOT guessed and "
                "the alarm list is NOT scanned: a wrong alarm returns another service's metrics "
                "under this incident's heading, and scanning every alarm takes ~26s.")

    if cw["alarm_name"] and out["window"]:
        alert_utc, how = to_utc(out["window"]["alert_time"], out["window"]["timezone"])
        if alert_utc is None:
            cw["refusals"].append(f"BLOCKING for the metric branch: {how}")
        else:
            cw["runnable"] = True
            # Filled in once `get_alarm` gives us the Period/EvaluationPeriods it evaluated over.
            cw["alert_utc"] = alert_utc.strftime(_METRIC_TIME_FORMAT)
            cw["conversion"] = (
                f"converted to UTC via {how}. This branch DOES convert the moment, unlike the log "
                f"branch, because CloudWatch takes UTC start/end times.")
    elif cw["alarm_name"] and not out["window"]:
        cw["refusals"].append(
            "the alarm name is known but no time window could be built, so no metric was queried. "
            "A metric window is never built from `now()` — it is built around the alert.")

    # Runnable, not merely "we understood the alert". Identifying the service is necessary and not
    # sufficient: this tool's real parameter is an ALERT TIME it backtracks from, so without a
    # window there is no honest query to send. The plan used to stay ok=True here and the
    # investigation went ahead untimed (intranet, 2026-07-31) — the refusal was reported while the
    # calls were made anyway, which is the worst of both: production reads, and an answer nobody
    # can scope.
    out["ok"] = bool(out["targets"] or parsed["use_cases"]) and bool(out["window"])
    # Either branch alone is enough to be worth running. A CloudWatch failure must not break a log
    # investigation that works, and the reverse holds too.
    out["any_runnable"] = bool(out["ok"] or cw["runnable"])
    return out


# ---- the investigation ----------------------------------------------------------------------

def _evidence_from_lines(lines, keyword, source, app, log_file, counts, owner="", window=None,
                         reported_count=None):
    """The log LINES -> an aggregate. They are local and stay local.

    Takes lines rather than a response body on purpose: extracting them is now shape-dependent (see
    `extract_log_lines`), and this function must never be reachable with a JSON blob that nobody
    decoded. When raw retention is on (UAT internal test only) the originals are handed to
    `incident_raw_store` and the evidence carries an opaque `ref` for the browser to fetch. The raw
    text still does not travel in this dict, so the model's view is unchanged either way.
    """
    lines = [line for line in (lines or []) if str(line).strip()]
    classes = {}
    for line in lines:
        for match in _EXCEPTION_CLASS.finditer(line):
            classes[match.group(1)] = classes.get(match.group(1), 0) + 1
    excerpts = [redact(line[:_MAX_EXCERPT_CHARS], counts) for line in lines[:_MAX_EXCERPTS]]
    ref = incident_raw_store.put(owner, lines, meta={
        "app": app, "source": source, "keyword": keyword, "file": log_file,
        "window": window or {}, "environment": "production"})
    return {
        "raw_ref": ref,
        "raw_ref_note": (
            "click-through to the original lines is available (UAT internal test). The raw text is "
            "NOT in this packet and you cannot read it — only the browser can fetch it. Tell the "
            "user they can expand it to verify; never claim to have read it yourself."
            if ref else
            "raw retention is off, so there is no original to click through to. The redacted "
            "excerpts are the whole record."),
        # The packet now carries two shapes of evidence (log lines and CloudWatch metrics), so each
        # says which it is rather than leaving a reader to infer it from the fields present.
        "kind": "log_lines",
        "source": source,
        "app": app,
        "file": log_file,
        # Both LogDream sources are production (owner, 2026-07-29). Labelled on every item so a
        # caller never has to infer it from the source name.
        "environment": "production" if source in log_sources() else "unknown",
        "matched_keyword": keyword,
        "lines_seen": len(lines),
        "lines_returned": len(excerpts),
        # Their own count when the response states one. Kept beside ours instead of replacing it:
        # a disagreement means either the response was truncated or we are reading the wrong field,
        # and both are things to say out loud rather than resolve by picking a number.
        **({"lines_reported_by_server": reported_count,
            "line_count_note": (
                "the server reported %d matching line(s) and this packet aggregates %d — the "
                "response was truncated, or the declared line field is wrong. Say the smaller "
                "number is what was actually read." % (reported_count, len(lines)))}
           if isinstance(reported_count, int) and reported_count != len(lines) else {}),
        "exception_classes": [name for name, _ in
                              sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))][:6],
        "excerpts": excerpts,
        "excerpt_policy": (f"redacted, first {_MAX_EXCERPTS} matching lines, "
                           f"{_MAX_EXCERPT_CHARS} chars each. The full response was held in memory "
                           f"and discarded; it is not retrievable from this packet."),
    }


def _cloudwatch_evidence(identity, window, points, status_code, counts):
    """Metric datapoints -> a categorised, redacted evidence item. The numbers stop here.

    What is deliberately absent: every datapoint, every aggregate (no average, min, max, latest or
    delta), the alarm ARN, its actions, its StateReasonData, and the raw dimension VALUES. The
    packet is persisted to the chat session, and a metric series taken from production is
    production data — the categories are what a reader actually needs.
    """
    return {
        "kind": "cloudwatch_metric",
        "environment": "production",
        "source": "cloudwatch",
        "metric": identity["metric"],
        "namespace": identity["namespace"],
        "statistic": identity["statistic"],
        "period_seconds": identity["period_seconds"],
        "window": {"start_utc": window["start_utc"], "end_utc": window["end_utc"],
                   "basis": window["basis"], "policy": window["policy"]},
        # Dimension NAMES identify the metric and are safe; the VALUES are service/cluster/customer
        # identifiers, so they are fingerprinted — same value still reads as the same value.
        "dimensions": [{"name": redact(d["Name"], counts), "value": f"<dim:{_digest(d['Value'])}>"}
                       for d in identity["dimensions"]],
        "status_code": status_code,
        "points_seen": len(points),
        "summary": summarize_points(points, identity.get("threshold"),
                                    identity.get("comparison") or ""),
        "reading_rule": (
            "These are CATEGORIES computed in memory from the datapoints; the values themselves are "
            "not in this packet and no tool returns them. They describe ONLY the window queried. "
            "`points_seen: 0` means no datapoint in exactly this window — NOT that the system was "
            "healthy. A direction is not a root cause."),
    }


# The abstract argument vocabulary this module speaks. The intranet maps each of these to the real
# parameter name in `config/mcp_tools.json`; nothing here ever names a real parameter.
#
# `alert_time` / `mode` / `backtrack_lines` replace the `from_time` / `to_time` the committed template
# still declares: the real `read_logdream_log` has no such parameters. It backtracks from an alert
# time, which is a different shape of question, not a renamed one.
READ_ARGS = ("app", "source", "file", "mode", "keyword", "alert_time", "timezone",
             "max_lines", "backtrack_lines")
SEARCH_ARGS = ("app", "source", "keyword", "date_hint", "filename_pattern")
# Required before a read can possibly succeed — the real tool cannot read without a file name.
READ_REQUIRED = ("app", "source", "file")
# Observed on the box (intranet report 2026-07-30). Only sent when `mode` is mapped AND the config's
# `const` does not already pin that parameter, so the box can override without a code change.
READ_MODE_BACKTRACK = os.environ.get("SDLC_INCIDENT_READ_MODE", "alert_time_backtrack")
BACKTRACK_LINES = int(os.environ.get("SDLC_INCIDENT_BACKTRACK_LINES", "200"))
_MAX_FILES_PER_SOURCE = int(os.environ.get("SDLC_INCIDENT_MAX_FILES", "2"))
# Real names confirmed in RUNBOOK-55. Used only to RANK candidates that the server returned — never
# to invent a file name, which is what hard-coding `otx_trace.log` amounted to.
_PREFERRED_LOG_FILES = ("otx_trace.log", "exception.log", "sftp.log")
_FILE_TOKEN = re.compile(r"[\w./\\-]*\.log(?:[._-]?\d{6,8})?")


def _usable_args(operation):
    """Abstract arg names this operation can actually pass right now.

    An arg declared as `"?"` is *known about* but unfilled, and `build_call` refuses it — so it is
    not usable. Distinguishing that from "not declared" is what lets this module send exactly what
    the current config supports and name precisely what is missing, instead of sending a doomed
    request or waiting for a config it cannot edit.
    """
    spec = mcp_registry.operations().get(operation) or {}
    args = spec.get("args") or {}
    return {name for name, target in args.items()
            if not str(name).startswith("_") and target and target != mcp_registry.UNSET}


def _pinned_params(operation):
    """Real parameter names the config pins via `const` — the box's override channel."""
    spec = mcp_registry.operations().get(operation) or {}
    const = spec.get("const") or {}
    return {str(name) for name in const if not str(name).startswith("_")}


def _payload(operation, wanted):
    """Keep only the args this operation can pass, and never fight a `const` the box has pinned."""
    spec = mcp_registry.operations().get(operation) or {}
    arg_map = spec.get("args") or {}
    usable = _usable_args(operation)
    pinned = _pinned_params(operation)
    return {name: value for name, value in wanted.items()
            if value not in (None, "") and name in usable and arg_map.get(name) not in pinned}


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


def _step(step, label, **detail):
    """One progress event.

    Passed through the SAME exit gate as the packet before it leaves. Progress lines are built from
    structured fields only (app, source, keyword, counts), but they are streamed straight to a
    browser, so they are the one place where a redaction miss would be visible before anybody could
    review it. Gating them costs nothing and removes the need to reason about it per call site.
    """
    event = {"type": "subagent_step", "step": step, "label": label, "detail": detail}
    cleaned, _report = sanitize_packet(event)
    return cleaned


def investigate(alert_text, **kwargs):
    """Run the plan and return the sanitized evidence packet (drains `investigate_events`)."""
    packet = {}
    for event in investigate_events(alert_text, **kwargs):
        if event.get("type") == "result":
            packet = event["packet"]
    return packet


def _cloudwatch_branch(query_plan, packet, counts):
    """The metric half: alarm -> its own metric identity -> a bounded UTC window -> categories.

    A generator that yields progress steps and writes into `packet`. It NEVER raises and never
    returns early out of the whole investigation: a CloudWatch failure has to leave a working log
    investigation alone (intranet handoff §16), so every outcome here ends in a recorded reason.
    """
    cw = query_plan.get("cloudwatch") or {}
    log = packet["cloudwatch_queries"]

    def _record(bucket, operation, args_sent, **extra):
        log[bucket].append({"server": "cloudwatch", "operation": operation,
                            "args_sent": sorted(args_sent), **extra})

    if cw.get("refusals"):
        packet["not_investigated"].extend(cw["refusals"])
    if not cw.get("runnable"):
        if cw.get("alarm_name") or cw.get("refusals"):
            yield _step("alarm_resolve", "CloudWatch 指标分支跳过（告警名或时间窗不确定），不猜",
                        resolved=False)
        return

    yield _step("alarm_resolve", "确定告警名（%s）" % cw.get("alarm_name_source") or "",
                resolved=True, source=cw.get("alarm_name_source"))

    # ---- 1. the alarm's own configuration ------------------------------------------------------
    yield _step("alarm_lookup", "取告警配置：命名空间 / 指标 / 维度 / 统计量 / 周期",
                server="cloudwatch", operation="aws.get_alarm")
    _record("attempted", "aws.get_alarm", ["alarm_name"])
    try:
        out = mcp_client.call("aws.get_alarm", {"alarm_name": cw["alarm_name"]})
    except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
        _record("failed", "aws.get_alarm", ["alarm_name"], refused_locally=True,
                reason=redact(str(exc)[:200], counts))
        packet["not_investigated"].append(
            f"the CloudWatch alarm lookup is not wired ({exc}), so no metric was read. The log "
            f"investigation is unaffected.")
        yield _step("alarm_lookup_failed", "告警配置查询未接通（未发出请求）",
                    server="cloudwatch", operation="aws.get_alarm", refused_locally=True)
        return
    except mcp_client.TransportError as exc:
        _record("failed", "aws.get_alarm", ["alarm_name"], refused_locally=False,
                reason=redact(str(exc)[:200], counts))
        packet["not_investigated"].append(
            f"CloudWatch did not respond to the alarm lookup ({exc}). This is NOT evidence that the "
            f"alarm does not exist, and no metric was read.")
        yield _step("alarm_lookup_failed", "CloudWatch 没响应 —— 这不等于“告警不存在”",
                    server="cloudwatch", operation="aws.get_alarm")
        return

    _record("executed", "aws.get_alarm", ["alarm_name"], elapsed_ms=out.get("elapsed_ms"))
    outcome, text = _tool_outcome(out)
    if outcome != "hit":
        reason = redact(text[:200], counts) if outcome == "error" else "an empty response"
        packet["not_investigated"].append(
            f"the alarm lookup returned {'an error' if outcome == 'error' else 'nothing'} "
            f"({reason}). No metric was read, and this is not evidence about the service.")
        yield _step("alarm_lookup_failed", "告警配置查询失败，不作为证据",
                    server="cloudwatch", operation="aws.get_alarm", rejected=True,
                    elapsed_ms=out.get("elapsed_ms"))
        return

    identity, why = alarm_metric_identity(_decode(text, out.get("structured")))
    if identity is None:
        packet["not_investigated"].append(
            f"the alarm was found but its metric identity could not be read: {why}. Nothing was "
            f"inferred from the alarm name, the repo or the resource string — a guessed namespace "
            f"or dimension returns a DIFFERENT service's numbers under this incident's heading.")
        yield _step("alarm_lookup_failed", "告警配置读不出指标身份，不猜命名空间/维度",
                    server="cloudwatch", operation="aws.get_alarm", shape_error=True)
        return

    # ---- 2. the window, built around the ALERT and never around now() --------------------------
    alert_utc = datetime.strptime(cw["alert_utc"], _METRIC_TIME_FORMAT).replace(tzinfo=_utc_tz.utc)
    window = metric_window_bounds(alert_utc, identity["period_seconds"],
                                  identity["evaluation_periods"])
    packet["cloudwatch_window"] = dict(window, timezone_conversion=cw.get("conversion", ""))

    args = {"namespace": identity["namespace"], "metric": identity["metric"],
            "dimensions": identity["dimensions"], "statistic": identity["statistic"],
            "period_seconds": identity["period_seconds"],
            "from_time": window["start_utc"], "to_time": window["end_utc"]}
    payload = _payload("aws.metric_window", args)
    # Metric identity is not optional. If the config cannot pass namespace/metric/times, sending a
    # partial request would query something other than what the alarm watches.
    missing = [name for name in ("namespace", "metric", "from_time", "to_time")
               if name not in payload]
    if missing:
        _record("failed", "aws.metric_window", payload, refused_locally=True,
                reason=f"config does not map {', '.join(missing)}")
        packet["not_investigated"].append(
            f"aws.metric_window cannot be called: config/mcp_tools.json does not map "
            f"{', '.join(missing)}. Nothing was queried — a partial metric request would return a "
            f"different metric, not a smaller answer.")
        yield _step("metric_window_failed", "指标查询缺参数映射：%s（未发出请求）" % "、".join(missing),
                    server="cloudwatch", operation="aws.metric_window", refused_locally=True,
                    missing=missing)
        return

    yield _step("metric_window", "取指标窗口：%s / %s（告警前后有界窗口，UTC）" % (
        identity["namespace"], identity["metric"]),
        server="cloudwatch", operation="aws.metric_window",
        namespace=identity["namespace"], metric=identity["metric"],
        statistic=identity["statistic"], period_seconds=identity["period_seconds"],
        start_utc=window["start_utc"], end_utc=window["end_utc"],
        args_sent=sorted(payload))
    _record("attempted", "aws.metric_window", payload)
    try:
        out = mcp_client.call("aws.metric_window", payload)
    except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
        _record("failed", "aws.metric_window", payload, refused_locally=True,
                reason=redact(str(exc)[:200], counts))
        packet["not_investigated"].append(f"aws.metric_window is not callable: {exc}.")
        yield _step("metric_window_failed", "指标查询被本地拒绝（未发出请求）",
                    server="cloudwatch", operation="aws.metric_window", refused_locally=True)
        return
    except mcp_client.TransportError as exc:
        _record("failed", "aws.metric_window", payload, refused_locally=False,
                reason=redact(str(exc)[:200], counts))
        packet["not_investigated"].append(
            f"CloudWatch did not respond to the metric query ({exc}). NO metric was obtained — this "
            f"is not 'the metric was normal'.")
        yield _step("metric_window_failed", "指标查询没响应 —— 这不等于“指标正常”",
                    server="cloudwatch", operation="aws.metric_window")
        return

    _record("executed", "aws.metric_window", payload, elapsed_ms=out.get("elapsed_ms"))
    outcome, text = _tool_outcome(out)
    if outcome == "error":
        _record("failed", "aws.metric_window", payload, refused_locally=False,
                reason=redact(text[:200], counts))
        packet["not_investigated"].append(
            f"the metric tool reported an error ({redact(text[:200], counts)}). Its message is not a "
            f"datapoint and not evidence.")
        yield _step("metric_window_failed", "指标工具报错，不作为证据",
                    server="cloudwatch", operation="aws.metric_window", rejected=True,
                    elapsed_ms=out.get("elapsed_ms"))
        return

    points, status_code, error = parse_metric_window(_decode(text, out.get("structured")))
    if points is None:
        packet["not_investigated"].append(
            f"the metric query SUCCEEDED but its response could not be read: {error}. This is our "
            f"parser/wiring, NOT a quiet metric — do not report it as 'no anomaly'. Declare the "
            f"real field names under operations['aws.metric_window'].response in mcp_tools.json.")
        yield _step("metric_window_failed", "指标返回体格式看不懂，不作为证据（查询本身成功）",
                    server="cloudwatch", operation="aws.metric_window", shape_error=True,
                    elapsed_ms=out.get("elapsed_ms"))
        return

    if not points:
        packet["not_investigated"].append(
            f"CloudWatch returned NO datapoint for {identity['namespace']}/{identity['metric']} "
            f"between {window['start_utc']} and {window['end_utc']}. That is a fact about this "
            f"exact window — it does NOT mean the service was healthy, and it is a normal result "
            f"when a metric is only published while traffic flows.")
        yield _step("metric_window_empty", "该时间窗内没有数据点 —— 这不等于“系统正常”",
                    server="cloudwatch", operation="aws.metric_window",
                    namespace=identity["namespace"], metric=identity["metric"],
                    points_seen=0, elapsed_ms=out.get("elapsed_ms"))
        return

    item = _cloudwatch_evidence(identity, window, points, status_code, counts)
    packet["evidence"].append(item)
    packet["contains_production_data"] = True
    yield _step("metric_evidence", "指标窗口：%d 个数据点，趋势 %s，波动 %s" % (
        item["points_seen"], item["summary"]["direction"], item["summary"]["variability"]),
        server="cloudwatch", operation="aws.metric_window",
        namespace=identity["namespace"], metric=identity["metric"],
        points_seen=item["points_seen"], summary=item["summary"],
        elapsed_ms=out.get("elapsed_ms"))


def investigate_events(alert_text, repos=None, timezone=None, query_plan=None, keywords=None,
                        sources=None, max_queries=None, owner="", alert_time=None,
                        alarm_name=None):
    """Run the plan against the log MCP, narrating each step, then yield the evidence packet.

    A generator rather than a callback so the caller can relay progress to a browser without threads
    or queues: the agent loop simply forwards each `subagent_step` and keeps the terminal `result`.
    The user watching a 30-second log sweep should see WHICH app, source and keyword is being spent —
    an opaque spinner is what makes people distrust an agent that is actually working correctly.

    Never yields raw log text. Never raises for an unreachable server — an incident answer needs to
    say "the log service did not respond" as a finding, not fall over.
    """
    counts = {}
    budget = max(1, int(max_queries or _MAX_LOG_QUERIES))
    yield _step("plan", "读告警：识别服务 / 用例，推导查询计划")
    query_plan = query_plan or plan(alert_text, repos=repos, timezone=timezone,
                                    keywords=keywords, sources=sources, alert_time=alert_time,
                                    alarm_name=alarm_name)
    if query_plan.get("targets") or query_plan.get("keywords"):
        yield _step(
            "plan_done",
            "计划就绪：%d 个服务，%d 个关键词，时间窗 %s" % (
                len(query_plan.get("targets") or []),
                len(query_plan.get("keywords") or []),
                (query_plan.get("window") or {}).get("timezone") or "未确定"),
            repos=[t["repo"] for t in query_plan.get("targets") or []],
            keywords=[k["term"] for k in query_plan.get("keywords") or []],
            window=query_plan.get("window"))
    packet = {
        "ok": False,
        "plan": query_plan,
        "evidence": [],
        # Exactly which app/source/keyword combinations were spent. A nil result is only meaningful
        # against this list, so it travels with the packet rather than being reconstructable.
        # Split three ways on purpose. One `queries_run` list, written BEFORE the request, made a
        # locally-refused call look queried — so "we asked and found nothing" and "we never asked"
        # became indistinguishable, which is the same confusion this whole module exists to prevent.
        "queries_attempted": [],
        "queries_executed": [],
        "queries_failed": [],
        # CloudWatch is accounted separately rather than squeezed into the log shape: an
        # app/source/file/keyword tuple is meaningless for a metric, and a fake one would make the
        # two branches impossible to tell apart when reading what was actually spent.
        "cloudwatch_queries": {"attempted": [], "executed": [], "failed": []},
        "not_investigated": [],
        "contains_production_data": False,
        "environments": {
            # Built from the configured names: the last hand-written copy said `hk1` for `hkl`.
            "logs": "production (LogDream %s — all production, different content)"
                    % " + ".join(log_sources()),
            "metrics": "production (CloudWatch, queried in UTC around the alert time)",
            "route_snapshot": "dev/SCT — absence there is NOT evidence of absence in production",
        },
        "caveats": [],
    }
    if not query_plan.get("any_runnable", query_plan.get("ok")):
        # Neither branch is runnable. Zero MCP calls from here — the refusals ARE the answer, and
        # every one of them is a question for the user rather than something to work around.
        packet["not_investigated"] = list(query_plan.get("refusals") or []) + list(
            (query_plan.get("cloudwatch") or {}).get("refusals") or [])
        packet["caveats"].append("nothing was queried; see not_investigated")
        yield _step("refused",
                    "拒绝调查：时间窗建不出来（缺日期或时区），一条日志都没查"
                    if query_plan.get("targets") and not query_plan.get("window")
                    else "拒绝调查：告警里读不出服务或用例，不猜",
                    reasons=packet["not_investigated"])
        yield {"type": "result", "packet": _finish(packet, counts)}
        return

    if not config.MCP_ENABLED:
        packet["not_investigated"].append(
            "production querying is switched off (SDLC_MCP_ENABLED unset), so this answer rests on "
            "local artefacts only — no logs and no metrics were read. The blast-radius answer does "
            "not need it; a root-cause answer does.")
        packet["caveats"].append("no production data was read")
        yield _step("disabled", "生产查询开关未开（SDLC_MCP_ENABLED），日志和指标都没读")
        yield {"type": "result", "packet": _finish(packet, counts)}
        return

    # ---- CloudWatch first: it is 2 calls, it needs no app-name mapping, and it must not be
    # skipped when the log branch stops early on an unwired read or an unresolvable app name.
    for event in _cloudwatch_branch(query_plan, packet, counts):
        yield event
    # Everything the metric branch could not do is already recorded. The log branch's own "we ran
    # and found nothing" verdict has to be judged against ITS reasons only — a CloudWatch refusal
    # saying nothing about the logs must not silently suppress it.
    cloudwatch_notes = len(packet["not_investigated"])

    if not query_plan.get("ok"):
        # The metric branch ran (or recorded why it did not); the log branch cannot.
        packet["not_investigated"].extend(query_plan.get("refusals") or [])
        yield _step("refused", "日志分支不可运行（服务或时间窗不确定），只跑了指标分支",
                    reasons=list(query_plan.get("refusals") or []))
        yield {"type": "result", "packet": _finish(packet, counts)}
        return

    # Their own app list is the only authority on app names — ours are candidates (RUNBOOK-55).
    # Queried PER SOURCE, for two reasons: the real `list_logdream_apps` requires a `source`, and the
    # two sources hold different apps, so a single merged list would send queries to a source that has
    # never heard of the app. A source whose listing fails is dropped and named, which is also how a
    # wrong source name in the config surfaces loudly instead of losing half the coverage silently.
    apps_by_source = {}
    supports_source = "source" in (mcp_registry.operations().get("log.list_apps") or {}).get(
        "args", {})
    for source in query_plan["sources"]:
        yield _step("apps", "取 %s 的应用清单（我们推的名字只是候选，以服务器为准）" % source,
                    server="logdream", operation="log.list_apps", source=source)
        try:
            listing = mcp_client.call("log.list_apps", {"source": source} if supports_source else None)
        except (mcp_client.Disabled, mcp_client.TransportError,
                mcp_registry.NotAllowed, mcp_registry.NotWired) as exc:
            packet["not_investigated"].append(
                f"could not list apps on source {source!r}: {exc}. Nothing was searched there.")
            yield _step("apps_failed", "%s 的应用清单取不到，该 source 不查" % source,
                        server="logdream", operation="log.list_apps", source=source,
                        error=str(exc))
            continue
        outcome, text = _tool_outcome(listing)
        if outcome == "error":
            # The tool ran and refused — e.g. an unknown source name. Its error body is non-empty, so
            # without this branch it would be split on whitespace and become "app names".
            packet["not_investigated"].append(
                f"source {source!r} was REJECTED by LogDream ({redact(text[:200], counts)}). Nothing was searched "
                f"there. If the name is wrong, fix `servers.logdream.sources` in the intranet's "
                f"mcp_tools.json — a bad source name otherwise costs half the log coverage silently.")
            yield _step("apps_failed", "%s 被服务器拒绝，该 source 不查（可能是 source 名写错）" % source,
                        server="logdream", operation="log.list_apps", source=source,
                        rejected=True, elapsed_ms=listing.get("elapsed_ms"))
            continue
        names, note, error = extract_app_names(text, listing.get("structured"))
        if names is None:
            # The listing succeeded but we cannot read its shape. Treated exactly like a source that
            # refused: no app on this source is verified, so nothing on it is queried. The old code
            # split the body on punctuation here and turned `entries`/`entry_type`/`README.txt` into
            # app names — a fabricated app verifies a candidate that does not exist, the read comes
            # back empty, and empty reads as "no problem".
            packet["not_investigated"].append(f"source {source!r}: {error} Nothing was searched there.")
            yield _step("apps_failed", "%s 的应用清单格式看不懂，该 source 不查（不猜应用名）" % source,
                        server="logdream", operation="log.list_apps", source=source,
                        shape_error=True, elapsed_ms=listing.get("elapsed_ms"))
            continue
        apps_by_source[source] = set(names)
        if note:
            packet["not_investigated"].append(f"source {source!r} app listing: {note}.")
        yield _step("apps_done", "%s 上有 %d 个应用" % (source, len(names)),
                    server="logdream", operation="log.list_apps", source=source,
                    app_count=len(names), note=note or None,
                    elapsed_ms=listing.get("elapsed_ms"))

    if not apps_by_source:
        packet["caveats"].append(
            "app names could not be verified on ANY source, so no log query was attempted — "
            "querying a guessed app name returns an empty result that reads like 'no problem'.")
        yield _step("apps_failed", "所有 source 都取不到应用清单，因此一条日志都没查",
                    server="logdream", operation="log.list_apps")
        yield {"type": "result", "packet": _finish(packet, counts)}
        return
    # Only search sources that actually answered.
    query_plan["sources_searched"] = sorted(apps_by_source)

    for target in query_plan["targets"]:
        # An app exists per SOURCE, not globally: the two sources hold different apps, so resolve the
        # candidate against each list and only query the sources that actually have it.
        match, on_sources = "", []
        for candidate in target["app_candidates"]:
            hosts = sorted(s for s, apps in apps_by_source.items() if candidate["app"] in apps)
            if hosts:
                match, on_sources = candidate["app"], hosts
                break
        if not match:
            target["app_note"] = (
                "none of the candidate app names exist on any searched source ("
                + ", ".join(sorted(apps_by_source)) + "): "
                + ", ".join(c["app"] for c in target["app_candidates"])
                + ". Not queried. This repo needs an entry in the intranet's "
                  "config/logdream_apps.json.")
            packet["not_investigated"].append(f"{target['repo']}: {target['app_note']}")
            yield _step("app_unresolved",
                        "%s：候选应用名在任何 source 上都不存在，跳过不查" % target["repo"],
                        repo=target["repo"],
                        candidates=[c["app"] for c in target["app_candidates"]])
            continue
        target["app_resolved"] = match
        target["app_on_sources"] = on_sources
        missing_on = sorted(set(apps_by_source) - set(on_sources))
        if missing_on:
            # Not a failure — the sources genuinely hold different apps — but it bounds the answer,
            # so it is stated rather than left for someone to assume full coverage.
            target["app_note"] = (f"app {match!r} exists on {', '.join(on_sources)} but NOT on "
                                  f"{', '.join(missing_on)}; those sources were not searched for it.")
            packet["not_investigated"].append(f"{target['repo']}: {target['app_note']}")
        yield _step("app_resolved",
                    "%s → 应用 %s（在 %s 上核对到）" % (target["repo"], match, "、".join(on_sources)),
                    repo=target["repo"], app=match, sources=on_sources)
        # ---- the hop that was missing: find the real log files before reading one ----------------
        window = query_plan.get("window") or {}
        # The NORMALIZED stamp, not the alert's own words: the real tool rejects
        # `2026-07-30 03:15 HKT` and wants the zone as its own parameter (intranet, 2026-07-31).
        alert_at = window.get("alert_time") or ""
        alert_date = alert_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", alert_at) else ""
        files_by_source = {}
        for source in on_sources:
            search_payload = _payload("log.search_files", {
                "app": match, "source": source,
                "keyword": (query_plan["keywords"][0]["term"] if query_plan["keywords"] else None),
                "date_hint": alert_date or None})
            yield _step("search_files", "在 %s / %s 上找候选日志文件" % (match, source),
                        server="logdream", operation="log.search_files",
                        app=match, source=source, args_sent=sorted(search_payload))
            try:
                found = mcp_client.call("log.search_files", search_payload)
            except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
                packet["not_investigated"].append(
                    f"{match}/{source}: log.search_files is not wired ({exc}), so no file name could "
                    f"be determined and nothing was read. The real read tool REQUIRES a file name.")
                yield _step("unwired", "%s / %s：文件搜索未接通，无法确定文件名" % (match, source),
                            server="logdream", operation="log.search_files", refused_locally=True)
                continue
            except mcp_client.TransportError as exc:
                packet["not_investigated"].append(
                    f"{match}/{source}: file search did not respond ({exc}). Nothing was read — this "
                    f"is NOT evidence of no matching lines.")
                yield _step("query_failed", "%s / %s：文件搜索没响应" % (match, source),
                            server="logdream", operation="log.search_files",
                            app=match, source=source)
                continue
            outcome, text = _tool_outcome(found)
            if outcome == "error":
                packet["not_investigated"].append(
                    f"{match}/{source}: the file-search tool REPORTED AN ERROR ({redact(text[:200], counts)}). "
                    f"Nothing was read.")
                yield _step("query_rejected", "%s / %s：文件搜索工具报错" % (match, source),
                            server="logdream", operation="log.search_files",
                            app=match, source=source, rejected=True,
                            elapsed_ms=found.get("elapsed_ms"))
                continue
            picked = select_log_files(text, alert_date=alert_date,
                                      structured=found.get("structured"))
            if not picked:
                packet["not_investigated"].append(
                    f"{match}/{source}: the file search returned no recognisable log file, so "
                    f"nothing was read. A file name is never guessed.")
                yield _step("no_files", "%s / %s：没找到可用的日志文件，不读" % (match, source),
                            server="logdream", operation="log.search_files",
                            app=match, source=source, elapsed_ms=found.get("elapsed_ms"))
                continue
            files_by_source[source] = picked
            target.setdefault("files", {})[source] = picked
            yield _step("files_found", "%s / %s：选中 %s" % (match, source, "、".join(picked)),
                        server="logdream", operation="log.search_files",
                        app=match, source=source, files=picked,
                        elapsed_ms=found.get("elapsed_ms"))

        if not files_by_source:
            continue

        # A read needs a file name; if the config cannot pass one, say exactly what to add.
        missing_map = [name for name in READ_REQUIRED if name not in _usable_args("log.read")]
        if missing_map:
            packet["not_investigated"].append(
                f"log.read cannot be called: config/mcp_tools.json does not map "
                f"{', '.join(missing_map)} (still `?` or absent). The intranet fills these from a "
                f"live tools/list; until then no log can be read.")
            packet["caveats"].append("the log read operation is not fully wired yet")
            yield _step("unwired",
                        "log.read 缺少参数映射：%s（未发出请求）" % "、".join(missing_map),
                        server="logdream", operation="log.read", refused_locally=True,
                        missing=missing_map)
            yield {"type": "result", "packet": _finish(packet, counts)}
            return

        # Built up front and then truncated, so what got SKIPPED is exact rather than inferred from
        # where a loop happened to stop.
        wanted = [(keyword["term"], source, log_file)
                  for keyword in query_plan["keywords"]
                  for source in sorted(files_by_source)
                  for log_file in files_by_source[source]]
        budget_left = max(0, budget - len(packet["queries_executed"]))
        running, skipped = wanted[:budget_left], wanted[budget_left:]
        if skipped:
            packet["not_investigated"].append(
                f"{match}: the {budget}-read query budget ran out, so these "
                f"keyword/source/file combinations were never tried: "
                + ", ".join(f"{term} on {source}:{log_file}" for term, source, log_file in skipped)
                + ". Raise SDLC_INCIDENT_MAX_LOG_QUERIES or narrow the keywords — do NOT read this "
                  "as 'those keywords found nothing'.")
            yield _step("budget_spent",
                        "查询预算用完（%d 次），%d 个关键词/文件组合没查" % (budget, len(skipped)),
                        budget=budget,
                        skipped=[f"{term}@{source}:{f}" for term, source, f in skipped])
        for index, (term, source, log_file) in enumerate(running, 1):
            yield _step("query", "读 %s / %s / %s：关键词 %s（%d/%d）" % (
                match, source, log_file, term, index, len(running)),
                server="logdream", operation="log.read",
                app=match, source=source, keyword=term, file=log_file)
            args = _payload("log.read", {
                "app": match, "source": source, "file": log_file, "keyword": term,
                # The real tool backtracks from an alert time; it has no from/to window. The stamp
                # goes as `YYYY-MM-DD HH:MM:SS` with the zone in its OWN parameter — the moment is
                # reformatted, never converted.
                "alert_time": alert_at or None,
                "timezone": window.get("timezone") or None,
                "mode": READ_MODE_BACKTRACK if alert_at else None,
                "backtrack_lines": BACKTRACK_LINES if alert_at else None,
            })
            attempt = {"app": match, "source": source, "keyword": term, "file": log_file,
                       "args_sent": sorted(args)}
            # Recorded as ATTEMPTED here and promoted to executed only once a response comes back.
            # Counting before the request meant a locally-refused call still looked queried.
            packet["queries_attempted"].append(attempt)
            try:
                out = mcp_client.call("log.read", args)
            except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
                # Refused by the allow-list / naming seam, NOT a failure to reach the server. Ops
                # need these apart: one is "nobody finished wiring this", the other is "the log
                # service is down", and they get escalated to different people. Never sent, so it
                # stays out of `queries_executed`.
                packet["queries_failed"].append({**attempt, "reason": str(exc),
                                                  "refused_locally": True})
                packet["not_investigated"].append(f"log.read unavailable: {exc}")
                packet["caveats"].append("the log read operation is not fully wired yet")
                yield _step("unwired", "日志读取操作还没接通完（被本地白名单/命名层拒绝，未发出请求）：%s"
                            % exc, server="logdream", operation="log.read", refused_locally=True)
                yield {"type": "result", "packet": _finish(packet, counts)}
                return
            except mcp_client.TransportError as exc:
                packet["queries_failed"].append({**attempt, "reason": str(exc),
                                                  "refused_locally": False})
                packet["not_investigated"].append(
                    f"{match}/{source}:{log_file} keyword {term!r}: log service did not respond "
                    f"({exc}). This is NOT evidence of no matching lines.")
                yield _step("query_failed",
                            "%s / %s / %s 没响应 —— 这不等于“没有匹配的日志”" % (
                                match, source, log_file),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term, file=log_file)
                continue
            packet["queries_executed"].append(attempt)
            outcome, text = _tool_outcome(out)
            if outcome == "error":
                # The tool ran and reported failure. Its message is NON-EMPTY, so treating text as
                # content here would wrap "unknown source hkl" up as a log finding and report a failed
                # call as "we found logs" — the worst outcome this feature can produce.
                packet["queries_failed"].append({**attempt, "reason": redact(text[:200], counts),
                                                  "refused_locally": False})
                packet["not_investigated"].append(
                    f"{match}/{source}:{log_file} keyword {term!r}: the log tool REPORTED AN ERROR "
                    f"({redact(text[:200], counts)}). This is not a log finding and not evidence of no matching "
                    f"lines — the query did not succeed.")
                yield _step("query_rejected",
                            "%s / %s / %s：%s 工具报错，不作为日志证据" % (
                                match, source, log_file, term),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term, file=log_file, rejected=True,
                            elapsed_ms=out.get("elapsed_ms"))
                continue
            if outcome == "empty":
                yield _step("query_empty", "%s / %s / %s：%s 无匹配" % (
                    match, source, log_file, term),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term, file=log_file,
                            elapsed_ms=out.get("elapsed_ms"))
                continue
            # Structured body -> the actual log-line fields. Splitting the JSON source counted 11
            # "lines" for a 2-line response (intranet, 2026-07-31), and every number downstream —
            # lines_seen, exception classes, excerpts, the retained raw — was computed off that.
            lines, reported, shape_error = extract_log_lines(text, out.get("structured"))
            if lines is None:
                packet["not_investigated"].append(
                    f"{match}/{source}:{log_file} keyword {term!r}: {shape_error} The query "
                    f"SUCCEEDED — this is our parser, not an empty log, so do not report it as "
                    f"'nothing found'.")
                yield _step("query_unreadable",
                            "%s / %s / %s：返回体格式看不懂，不作为证据（查询本身是成功的）" % (
                                match, source, log_file),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term, file=log_file,
                            shape_error=True, elapsed_ms=out.get("elapsed_ms"))
                continue
            if not lines:
                yield _step("query_empty", "%s / %s / %s：%s 无匹配" % (
                    match, source, log_file, term),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term, file=log_file,
                            elapsed_ms=out.get("elapsed_ms"))
                continue
            # The file ACTUALLY read, not a hard-coded name: mislabelling `exception.log` as
            # `otx_trace.log` would misdirect whoever goes to check it.
            item = _evidence_from_lines(lines, term, source, match, log_file, counts,
                                        owner=owner, window=query_plan.get("window"),
                                        reported_count=reported)
            packet["evidence"].append(item)
            packet["contains_production_data"] = True
            # Counts and exception classes only — never a line, not even a redacted one. Excerpts
            # exist in the packet, which the model reads; the live stream does not need them.
            yield _step("evidence",
                        "%s / %s：命中 %d 行，异常类 %s" % (
                            match, source, item["lines_seen"],
                            "、".join(item["exception_classes"]) or "无"),
                        server="logdream", operation="log.read",
                        app=match, source=source, keyword=term,
                        lines_seen=item["lines_seen"],
                        exception_classes=item["exception_classes"],
                        elapsed_ms=out.get("elapsed_ms"),
                        truncated=bool(out.get("truncated")),
                        # Present only under raw retention. The browser fetches the original with
                        # it; nothing in the stream or the packet contains the text itself.
                        raw_ref=item["raw_ref"])

    packet["ok"] = bool(packet["evidence"]) or not packet["not_investigated"]
    log_notes = packet["not_investigated"][cloudwatch_notes:]
    if not packet["evidence"] and packet["queries_executed"] and not log_notes:
        packet["caveats"].append(
            "the queries ran and matched nothing. That is a real finding only for the keywords and "
            "window actually used — see `queries_executed`.")
    final = _finish(packet, counts)
    yield _step("summary", "调查完成：%d 条证据，日志读取 %d 次，指标查询 %d 次，%d 项没查（脱敏 %d 处）" % (
        len(final.get("evidence") or []), len(final.get("queries_executed") or []),
        len((final.get("cloudwatch_queries") or {}).get("executed") or []),
        len(final.get("not_investigated") or []),
        sum((final.get("redactions") or {}).values())),
        evidence=len(final.get("evidence") or []),
        executed=len(final.get("queries_executed") or []),
        failed=len(final.get("queries_failed") or []),
        not_investigated=len(final.get("not_investigated") or []),
        redactions=final.get("redactions") or {})
    yield {"type": "result", "packet": final}


def _fingerprint_alarm_name(node, name, marker):
    """Replace every occurrence of the alarm name with its fingerprint, anywhere in the packet.

    Whole-structure rather than one field on purpose: the same reasoning as the PII exit gate. One
    known field is easy to clear and easy for a future code path to reintroduce somewhere else.
    """
    if isinstance(node, str):
        return node.replace(name, marker)
    if isinstance(node, dict):
        return {key: _fingerprint_alarm_name(value, name, marker) for key, value in node.items()}
    if isinstance(node, list):
        return [_fingerprint_alarm_name(item, name, marker) for item in node]
    return node


def _scrub_alarm_name(cleaned):
    """The alarm name identifies a production service, so it leaves as a fingerprint.

    The intranet's UAT found the real name surviving in `plan.cloudwatch.alarm_name` (2026-07-31)
    — and observed that the only place a service identifier appeared in the clear was INSIDE that
    name, since alarm names embed the service they watch. Dimension values were already
    fingerprinted, so leaving the name whole was simply incoherent: the same identifier, masked in
    one field and printed in another.

    Nothing is lost for the reader. The alarm name came FROM the user, so it is already in the
    conversation; the packet does not need to be a second copy of it. The fingerprint is stable, so
    two investigations of the same alarm still read as the same alarm.
    """
    plan_block = (cleaned.get("plan") or {}).get("cloudwatch") or {}
    name = plan_block.get("alarm_name") or ""
    # A very short name could appear as a substring of unrelated prose; alarm names are not short.
    if len(name) < 4:
        return cleaned
    marker = f"<alarm:{_digest(name)}>"
    cleaned = _fingerprint_alarm_name(cleaned, name, marker)
    block = (cleaned.get("plan") or {}).get("cloudwatch")
    if isinstance(block, dict):
        block["alarm_name"] = marker
        block["alarm_name_note"] = (
            "fingerprinted at the exit — an alarm name embeds the service it watches, and this "
            "packet is persisted. The USER told you the alarm name, so refer to it as they wrote "
            "it; never invent one, and never treat this marker as the name to quote back.")
    return cleaned


def _finish(packet, counts):
    """Exit gate: defence 2, plus the accounting that makes the wall auditable."""
    packet["redactions"] = dict(sorted(counts.items()))
    cleaned, report = sanitize_packet(packet)
    # After the PII gate, before anything else looks at the packet: every return path goes through
    # here, so there is no branch where the raw name survives.
    cleaned = _scrub_alarm_name(cleaned)
    cleaned["exit_check"] = report
    if report["sanitized_at_exit"]:
        cleaned["caveats"] = list(cleaned.get("caveats") or []) + [
            f"{report['sanitized_at_exit']} field(s) still matched "
            f"{', '.join(report['kinds'])} at the exit gate and were removed. Redaction upstream "
            f"missed them — that is a bug worth reporting, not a data problem."]
    if cleaned.get("contains_production_data"):
        if incident_raw_store.enabled():
            cleaned["storage_rule"] = (
                "This packet contains material derived from PRODUCTION logs, redacted and bounded. "
                "RAW LOG RETENTION IS ON (UAT internal test): the original lines are retained "
                "separately and the user can click through to verify each evidence item. YOU cannot "
                "read them — the raw text is not in this packet and no tool returns it — so offer "
                "the click-through, and never imply you checked the original yourself.")
        else:
            cleaned["storage_rule"] = (
                "This packet contains material derived from PRODUCTION logs. It is redacted, bounded "
                "and safe to persist as-is; the underlying raw log text was never returned and is "
                "gone. Do not ask for it again in raw form — there is no code path that provides it.")
        cleaned["raw_retention"] = incident_raw_store.status()
    return cleaned
