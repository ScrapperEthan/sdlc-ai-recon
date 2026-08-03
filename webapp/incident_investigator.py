"""The incident sub-agent: reads production over MCP, returns a redacted evidence packet.

This module is the ORCHESTRATION and the EXIT GATE. The two layers under it were split out once it
passed 2700 lines, because they fail in different ways and are testable at different costs:

* `incident_plan`   — alert text -> a query plan. Decides what to ask. Opens no sockets.
* `incident_parse`  — reads the bodies that come back. Decides what an answer MEANS.
* here              — runs the three branches, shapes evidence, and sanitizes what leaves.

Everything moved is reached module-qualified — `incident_plan.plan(...)`,
`incident_parse._decode(...)` — never through a re-export. That is deliberate: `from X import f`
creates a SECOND binding, so patching `incident_plan.log_sources` in a test would be invisible here
and the test would quietly exercise the real function. One binding per name means patching and
reading can never disagree. Only the exit-gate names are imported directly, because this module is
the exit gate and they read as its own vocabulary.

## Three branches, ascending in what they need to know

PORTAL delivery records (a `tracking_id` alone), the CloudWatch metric around an alarm (an alarm
name plus a window), and LogDream logs (a repo/app plus a window). They run independently and any
one can succeed while the others refuse — one branch's silence never stands in for another's result.

## Everything fails closed

* An app name is only used if it appears in the server's OWN `log.list_apps` output. RUNBOOK-55
  measured repo->app naming at 0% identical and ~36% by rule, so a rule-derived name is a
  *candidate*, never an answer.
* A time window is never invented. Three timezones coexist (CloudWatch UTC / LogDream
  Asia/Hong_Kong / servers GMT); a helpfully-defaulted window returns nothing and reads as "no
  anomaly", which is the worst possible failure for this feature. Without one the plan is NOT
  runnable and zero calls are made.
* A structured response is read structurally. Their bodies are JSON; reading JSON as text is how a
  2-line response was reported as 11 lines and how `entries`/`entry_type` became "app names"
  (intranet, 2026-07-31). See `incident_parse`.
* Nothing leaves without crossing `redaction`. The model receives only the redacted packet; raw text
  goes to the owner-scoped side store or nowhere at all.
"""
import os
import re
from datetime import datetime
from datetime import timezone as _utc_tz          # `timezone` is a parameter name all over this file

from . import config, incident_raw_store, mcp_client, mcp_registry

# ---- the two layers under this one ------------------------------------------------------------
# Imported as MODULES and always called qualified. There is exactly one binding for each moved name,
# living in the module that owns it, so a test that patches `incident_plan.log_sources` is seen by
# every caller including this one. `log_sources` is called from both modules, and a `from ... import`
# here would have left this side bound to the original — a patched test silently exercising the real
# function is the worst kind of green.
from . import incident_parse, incident_plan
# The exception, and only because this module IS the exit gate: these read as its own vocabulary
# rather than as a call into another layer.
from .redaction import _digest, redact, sanitize_packet
# Constants used unqualified in the body below. Values, not behaviour — nothing patches them, so a
# second binding costs nothing.
from .incident_plan import _EXCEPTION_CLASS, _METRIC_TIME_FORMAT, _RESOURCE_DIMENSIONS
from .incident_parse import (
    _PORTAL_REASON_KEYS, _PORTAL_STATUS_KEYS, _TAG_KEYS_OF_INTEREST)


# How many history / change rows are read. Context, not a timeline — a bigger number would only add
# material we deliberately throw away.
_MAX_CONTEXT_ITEMS = int(os.environ.get("SDLC_INCIDENT_MAX_CONTEXT_ITEMS", "25"))


_MAX_EXCERPTS = 5
_MAX_EXCERPT_CHARS = 300
# Hard ceiling on log reads per investigation. Keywords x sources multiplies fast — 8 keywords over
# two production sources is 16 calls, and RUNBOOK-55 clocked a single MCP call at 26.4s, so an
# unbounded sweep is a seven-minute answer. Which keywords were actually spent is always reported,
# because a nil result is only meaningful for the queries that ran.
_MAX_LOG_QUERIES = int(os.environ.get("SDLC_INCIDENT_MAX_LOG_QUERIES", "6"))


# ---- the investigation ----------------------------------------------------------------------

def _evidence_from_lines(lines, keyword, source, app, log_file, counts, owner="", window=None,
                         reported_count=None):
    """The log LINES -> an aggregate. They are local and stay local.

    Takes lines rather than a response body on purpose: extracting them is now shape-dependent (see
    `incident_parse.extract_log_lines`), and this function must never be reachable with a JSON blob that nobody
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
        "environment": "production" if source in incident_plan.log_sources() else "unknown",
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
        "summary": incident_parse.summarize_points(points, identity.get("threshold"),
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


_TRANSPORT_ADVICE = {
    "connection_refused": ("the address answered and REFUSED the connection — that points at the "
                           "MCP service itself (not running, bound to loopback only, or a changed "
                           "port), not at the network. Nothing was read."),
    "dns_failure": ("the hostname could not be resolved. Nothing was read — this is a name/DNS "
                    "problem, not a quiet log."),
    "timeout": ("the request was sent and nothing came back in time. Whether it ran on their side "
                "is unknown, so treat this as 'we did not get an answer', never as 'no results'."),
    "connection_reset": "the connection was dropped mid-exchange. Nothing usable was read.",
    "tls_failure": "the TLS handshake failed. Nothing was read.",
}


def _transport_note(exc):
    """A short, address-free explanation of a transport failure, for `not_investigated`.

    The category matters operationally: the intranet spent a full diagnostic cycle establishing that
    LogDream's port was REFUSED rather than timing out, because those two go to different owners.
    Writing the distinction into the packet means the next reader starts where they finished.
    """
    kind = getattr(exc, "kind", "") or "unreachable"
    return _TRANSPORT_ADVICE.get(kind, "nothing was read.")


def _transport_meta(out):
    """Retry facts worth carrying out of an MCP result, or {} for the normal single-attempt case.

    A retried call is a fact about the NETWORK, not about the logs. RUNBOOK-65 caught one
    `getaddrinfo failed` that was healthy again three seconds later; a reader who cannot see that
    happened would read the surrounding result as a quiet system rather than a flaky link — the same
    "we did not look, and it reads like we did" confusion this module exists to prevent.
    """
    if not isinstance(out, dict) or not out.get("retried"):
        return {}
    return {"attempts": out.get("attempts"), "retried": True}


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


def _portal_evidence(body, channel, operation, tracking_ref):
    """Their record -> a category-only evidence item, or None when the shape is not recognised.

    Fails CLOSED. A forward record has only ever been exercised against a synthetic id that returns
    not-found (their §2.6), so an unrecognised shape is a wiring problem to fix in
    `config/mcp_tools.json` — it is emphatically NOT an empty record, and must never be reported as
    "nothing was delivered".
    """
    rows = body if isinstance(body, list) else [body]
    row = next((item for item in rows if isinstance(item, dict)), None)
    if row is None:
        return None
    lowered = {str(key).lower(): value for key, value in row.items()}
    status_value = next((lowered[key] for key in _PORTAL_STATUS_KEYS if key in lowered), None)
    if status_value is None:
        return None                        # shape not recognised -> caller reports a parser failure
    reason_value = next((lowered[key] for key in _PORTAL_REASON_KEYS if key in lowered), "")
    status = incident_parse._portal_status(status_value)
    return {
        "kind": "portal_delivery",
        "environment": "production",
        "source": "portal",
        "channel": channel,
        "operation": operation,
        "record_found": True,
        "delivery_status": status,
        "failure_category": incident_parse._portal_failure_kind(reason_value) if status == "failed" else "unknown",
        # Presence only — a timestamp is a fact about the record, its VALUE is not needed here.
        "timestamps_present": any("time" in key or "date" in key for key in lowered),
        "tracking_ref": tracking_ref,
        "note": ("category-only evidence; the raw Portal record was read in memory and not "
                 "persisted. Recipient, payload and message content are never extracted."),
    }


def _portal_branch(query_plan, packet, counts):
    """The delivery-record half: a tracking id -> Portal's SMS/Email record -> a category.

    A generator, like `_cloudwatch_branch`. It NEVER raises and never returns early out of the whole
    investigation — Portal failing has to leave the log and metric branches alone.
    """
    portal = query_plan.get("portal") or {}
    ledger = packet["portal_queries"]
    tracking_id = portal.get("tracking_id") or ""
    # The real id goes ONLY into the outbound request. Everything that can be shown, streamed or
    # stored gets the fingerprint — same rule as the alarm name (RUNBOOK-64) and the PII gate.
    tracking_ref = f"<tracking:{_digest(tracking_id)}>" if tracking_id else ""

    def _record(bucket, operation, args_sent, **extra):
        ledger[bucket].append({"server": "portal", "operation": operation,
                               "args_sent": sorted(args_sent), **extra})

    if portal.get("refusals"):
        packet["not_investigated"].extend(portal["refusals"])
    if not portal.get("runnable"):
        if portal.get("refusals"):
            yield _step("portal_skipped", "Portal 投递记录分支跳过（没有唯一的 tracking id），不猜",
                        resolved=False)
        return

    channel = portal.get("channel") or "auto"
    operations = {"sms": ["portal.sms_by_tracking_id"],
                  "email": ["portal.email_by_tracking_id"],
                  "auto": ["portal.sms_by_tracking_id", "portal.email_by_tracking_id"]}[channel]

    yield _step("portal_resolve", "Portal 分支：按 tracking id 查投递记录（%s）" % channel,
                source=portal.get("tracking_id_source"), tracking_ref=tracking_ref)

    for operation in operations:
        # Each operation is independent: SMS failing must not cost the Email lookup and vice versa.
        yield _step("portal_query", "查 %s" % operation, server="portal", operation=operation)
        _record("attempted", operation, ["tracking_id"])
        try:
            out = mcp_client.call(operation, {"tracking_id": tracking_id})
        except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
            _record("failed", operation, ["tracking_id"], refused_locally=True)
            packet["not_investigated"].append(f"{operation} is not callable: {exc}.")
            yield _step("unwired", f"{operation} 未接线，未发出请求", server="portal",
                        operation=operation, refused_locally=True)
            continue
        except mcp_client.TransportError as exc:
            _record("failed", operation, ["tracking_id"], error=str(exc)[:200])
            packet["not_investigated"].append(
                f"{operation} did not respond ({str(exc)[:160]}): {_transport_note(exc)} Portal was NOT "
                f"read for this channel — that is not the same as no delivery record.")
            yield _step("query_failed", f"{operation} 没有响应", server="portal",
                        operation=operation)
            continue

        _record("executed", operation, ["tracking_id"], elapsed_ms=out.get("elapsed_ms"),
                **_transport_meta(out))
        outcome, detail = incident_parse._tool_outcome(out)
        if outcome == "error":
            packet["not_investigated"].append(
                f"{operation} REPORTED AN ERROR: {redact(str(detail)[:200], counts)}. That is the "
                f"tool refusing, not a delivery record — never read it as 'not delivered'.")
            yield _step("query_rejected", f"{operation} 被拒绝", server="portal",
                        operation=operation)
            continue

        body = incident_parse._decode(out.get("text"), out.get("structured"))
        if outcome == "empty" or body in (None, [], {}):
            # A genuine not-found. It IS a result — and it is NOT proof the message was delivered,
            # nor proof there was no business impact.
            packet["evidence"].append({
                "kind": "portal_delivery", "environment": "production", "source": "portal",
                "channel": channel, "operation": operation, "record_found": False,
                "delivery_status": "unknown", "failure_category": "unknown",
                "tracking_ref": tracking_ref,
                "note": ("Portal has NO delivery record for this tracking id on this channel. That "
                         "is a query result, not evidence of successful delivery and not evidence "
                         "of no business impact — the id may belong to the other channel, to a "
                         "different environment, or be outside retention."),
            })
            yield _step("portal_empty", f"{operation}：无该 tracking id 的投递记录",
                        server="portal", operation=operation)
            continue

        item = _portal_evidence(body, channel, operation, tracking_ref)
        if item is None:
            shape = incident_parse.describe_shape(body)
            packet["not_investigated"].append(
                f"{operation} SUCCEEDED but our parser could not read the response shape "
                f"({shape}). This is OUR wiring gap — a `response` mapping in "
                f"config/mcp_tools.json — NOT an empty record. Do not report it as 'no delivery'.")
            yield _step("query_unreadable", f"{operation} 返回体读不懂（我方映射缺口）",
                        server="portal", operation=operation, shape_error=True)
            continue

        packet["evidence"].append(item)
        packet["contains_production_data"] = True
        yield _step("portal_evidence",
                    "命中投递记录：%s / %s" % (item["delivery_status"], item["failure_category"]),
                    server="portal", operation=operation)


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

    _record("executed", "aws.get_alarm", ["alarm_name"], elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
    outcome, text = incident_parse._tool_outcome(out)
    if outcome != "hit":
        reason = redact(text[:200], counts) if outcome == "error" else "an empty response"
        packet["not_investigated"].append(
            f"the alarm lookup returned {'an error' if outcome == 'error' else 'nothing'} "
            f"({reason}). No metric was read, and this is not evidence about the service.")
        yield _step("alarm_lookup_failed", "告警配置查询失败，不作为证据",
                    server="cloudwatch", operation="aws.get_alarm", rejected=True,
                    elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
        return

    identity, why = incident_parse.alarm_metric_identity(incident_parse._decode(text, out.get("structured")))
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
    window = incident_plan.metric_window_bounds(alert_utc, identity["period_seconds"],
                                  identity["evaluation_periods"])
    packet["cloudwatch_window"] = dict(window, timezone_conversion=cw.get("conversion", ""))

    # Everything above is the COMMON PREFIX: without an alarm identity and a window, none of the
    # three sub-branches below can run. From here down they are independent, and each is its own
    # generator so that its early `return` ends only ITSELF. That matters: this function used to
    # return outright when the metric read failed, which would have meant CloudWatch Logs never ran
    # on exactly the incidents where the metric was unavailable (intranet handoff §1.2).
    yield from _cloudwatch_metric(cw, identity, window, packet, counts)
    yield from _cloudwatch_context(cw, identity, window, packet, counts)
    yield from _cloudwatch_logs(cw, identity, window, query_plan, packet, counts)
    yield from _cloudwatch_tags(identity, packet, counts)


def _cloudwatch_metric(cw, identity, window, packet, counts):
    """The measurement itself. Categories out, raw datapoints discarded."""
    ledger = packet["cloudwatch_queries"]

    def _record(bucket, operation, args_sent, **extra):
        ledger[bucket].append({"server": "cloudwatch", "operation": operation,
                               "args_sent": sorted(args_sent), **extra})

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

    _record("executed", "aws.metric_window", payload, elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
    outcome, text = incident_parse._tool_outcome(out)
    if outcome == "error":
        _record("failed", "aws.metric_window", payload, refused_locally=False,
                reason=redact(text[:200], counts))
        packet["not_investigated"].append(
            f"the metric tool reported an error ({redact(text[:200], counts)}). Its message is not a "
            f"datapoint and not evidence.")
        yield _step("metric_window_failed", "指标工具报错，不作为证据",
                    server="cloudwatch", operation="aws.metric_window", rejected=True,
                    elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
        return

    points, status_code, error = incident_parse.parse_metric_window(incident_parse._decode(text, out.get("structured")))
    if points is None:
        packet["not_investigated"].append(
            f"the metric query SUCCEEDED but its response could not be read: {error}. This is our "
            f"parser/wiring, NOT a quiet metric — do not report it as 'no anomaly'. Declare the "
            f"real field names under operations['aws.metric_window'].response in mcp_tools.json.")
        yield _step("metric_window_failed", "指标返回体格式看不懂，不作为证据（查询本身成功）",
                    server="cloudwatch", operation="aws.metric_window", shape_error=True,
                    elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
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
                    points_seen=0, elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
        return

    item = _cloudwatch_evidence(identity, window, points, status_code, counts)
    packet["evidence"].append(item)
    packet["contains_production_data"] = True
    yield _step("metric_evidence", "指标窗口：%d 个数据点，趋势 %s，波动 %s" % (
        item["points_seen"], item["summary"]["direction"], item["summary"]["variability"]),
        server="cloudwatch", operation="aws.metric_window",
        namespace=identity["namespace"], metric=identity["metric"],
        points_seen=item["points_seen"], summary=item["summary"],
        elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))



# ---- CloudWatch Logs -------------------------------------------------------------------------
# The second log source. It matters because LogDream resolves an app for only a fraction of the
# estate (84/460 as of 2026-08-03) — for the rest, CloudWatch Logs is the only log evidence there
# is. Every bound below is a hard cap from the intranet handoff §1.5; none of them may be raised by
# a tool argument or by anything the model says.
_MAX_LOG_GROUPS = 5
_MAX_LOGS_KEYWORDS = 5
_MAX_LOGS_KEYWORD_CHARS = 80
_MAX_LOGS_LIMIT = 100
_MAX_LOGS_WINDOW_MINUTES = 36
# Regex metacharacters have no business in a keyword we derived from a repo name or an exception
# class; stripping them is simpler and safer than escaping, because a keyword that needs them is a
# keyword we did not intend to send.
_LOGS_KEYWORD_SAFE = re.compile(r"[^A-Za-z0-9_.\- ]+")


def _bounded_keyword(term):
    """One keyword, stripped to a safe literal and length-capped, or "" if nothing usable is left."""
    cleaned = _LOGS_KEYWORD_SAFE.sub(" ", str(term or "")).strip()
    return cleaned[:_MAX_LOGS_KEYWORD_CHARS].strip()


def _bounded_logs_keywords(query_plan):
    """At most `_MAX_LOGS_KEYWORDS` safe keywords from the plan, in plan order, de-duplicated."""
    out = []
    for item in (query_plan or {}).get("keywords") or []:
        term = _bounded_keyword(item.get("term") if isinstance(item, dict) else item)
        if term and term not in out:
            out.append(term)
        if len(out) >= _MAX_LOGS_KEYWORDS:
            break
    return out


def _fixed_logs_query(keyword):
    """The ONLY query string this code ever sends.

    Built here, from a template, with the keyword already reduced to a safe literal. The model never
    supplies a query and cannot influence `fields`, `sort` or `limit` — a Logs Insights query is a
    small language, and handing a language to the model is handing it the ability to read log groups
    and fields nobody scoped.
    """
    return ("fields @timestamp, @message "
            f"| filter @message like /{keyword}/ "
            "| sort @timestamp desc "
            f"| limit {_MAX_LOGS_LIMIT}")


def _explicit_resource_identity(identity):
    """Resource fields copied VERBATIM from `get_alarm`'s Dimensions, or blanks.

    `resource` maps to their `resourceName` and `resource_arn` to `resourceArn` — confirmed by the
    intranet 2026-08-03, and they are NOT interchangeable. `resource_type` is left empty on purpose:
    its value domain is unconfirmed, and inventing an enum value to make a request "more complete"
    is exactly the guess this seam exists to stop.
    """
    dimensions = (identity or {}).get("dimensions") or []
    if not isinstance(dimensions, list):
        return {"resource": "", "resource_type": "", "resource_arn": "", "dimension_name": ""}
    by_name = {str(item.get("Name") or ""): str(item.get("Value") or "")
               for item in dimensions if isinstance(item, dict)}
    for name in _RESOURCE_DIMENSIONS:
        value = by_name.get(name, "").strip()
        if not value:
            continue
        is_arn = value.startswith("arn:")
        return {"resource": "" if is_arn else value,
                "resource_type": "",
                "resource_arn": value if is_arn else "",
                "dimension_name": name}
    return {"resource": "", "resource_type": "", "resource_arn": "", "dimension_name": ""}


def _cloudwatch_logs(cw, identity, window, query_plan, packet, counts):
    """resource -> its log groups -> a bounded Insights query -> redacted, bounded excerpts."""
    ledger = packet["cloudwatch_logs"]

    def _record(bucket, operation, args_sent, **extra):
        ledger[bucket].append({"server": "cloudwatch", "operation": operation,
                               "args_sent": sorted(args_sent), **extra})

    resource = _explicit_resource_identity(identity)
    if not resource["resource"] and not resource["resource_arn"]:
        packet["not_investigated"].append(
            "CloudWatch Logs was skipped: get_alarm exposed no explicit resource name or ARN in its "
            "Dimensions, and a resource is NEVER assembled from the alarm name or a repo id. "
            "Nothing was guessed and nothing was queried — this is not 'the service has no logs'.")
        yield _step("cw_logs_skipped", "CloudWatch Logs 跳过：告警维度里没有明确资源，不猜",
                    resolved=False)
        return

    keywords = _bounded_logs_keywords(query_plan)
    if not keywords:
        packet["not_investigated"].append(
            "CloudWatch Logs was skipped: no usable keyword survived the plan. A query with no "
            "keyword would return whatever happened to be latest, which is not evidence.")
        return

    find_args = {"resource": resource["resource"], "resource_arn": resource["resource_arn"],
                 "resource_type": resource["resource_type"], "max_results": _MAX_LOG_GROUPS}
    payload = _payload("aws.log_groups_for_resource", find_args)
    yield _step("cw_log_groups", "找该资源的 log groups（维度 %s）" % resource["dimension_name"],
                server="cloudwatch", operation="aws.log_groups_for_resource")
    _record("attempted", "aws.log_groups_for_resource", payload)
    try:
        out = mcp_client.call("aws.log_groups_for_resource", payload)
    except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
        _record("failed", "aws.log_groups_for_resource", payload, refused_locally=True)
        packet["not_investigated"].append(f"aws.log_groups_for_resource is not callable: {exc}.")
        yield _step("unwired", "aws.log_groups_for_resource 未接线", server="cloudwatch",
                    operation="aws.log_groups_for_resource", refused_locally=True)
        return
    except mcp_client.TransportError as exc:
        _record("failed", "aws.log_groups_for_resource", payload, error=str(exc)[:200])
        packet["not_investigated"].append(
            f"aws.log_groups_for_resource did not respond ({str(exc)[:160]}): {_transport_note(exc)} "
            f"No CloudWatch log group was identified, so none was read.")
        yield _step("query_failed", "log group 查询没有响应", server="cloudwatch",
                    operation="aws.log_groups_for_resource")
        return

    _record("executed", "aws.log_groups_for_resource", payload,
            elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
    outcome, detail = incident_parse._tool_outcome(out)
    if outcome == "error":
        packet["not_investigated"].append(
            f"aws.log_groups_for_resource REPORTED AN ERROR: {redact(str(detail)[:200], counts)}. "
            f"Their tool refusing is not an absence of log groups.")
        yield _step("query_rejected", "log group 查询被拒绝", server="cloudwatch",
                    operation="aws.log_groups_for_resource")
        return

    body = incident_parse._decode(out.get("text"), out.get("structured"))
    groups = None if (outcome == "empty" or body in (None, "", [], {})) else incident_parse._parse_log_groups(body)
    if groups is None and outcome != "empty" and body not in (None, "", [], {}):
        packet["not_investigated"].append(
            f"aws.log_groups_for_resource SUCCEEDED but our parser could not read the response "
            f"shape ({incident_parse.describe_shape(body)}). OUR wiring gap — a `response` mapping in "
            f"config/mcp_tools.json — NOT an absence of log groups.")
        yield _step("query_unreadable", "log group 返回体读不懂（我方映射缺口）",
                    server="cloudwatch", operation="aws.log_groups_for_resource", shape_error=True)
        return
    groups = (groups or [])[:_MAX_LOG_GROUPS]
    if not groups:
        packet["not_investigated"].append(
            "aws.log_groups_for_resource returned no log group for this resource. That is a query "
            "result about the RESOURCE MAPPING, not evidence that the service writes no logs.")
        yield _step("cw_logs_empty", "该资源没有返回 log group", server="cloudwatch",
                    operation="aws.log_groups_for_resource")
        return

    # The Logs window is bounded independently of the metric window: a metric window widens with the
    # alarm's own evaluation periods, and a log scan must not inherit that.
    start, end = window.get("start_utc", ""), window.get("end_utc", "")
    matches, classes, excerpts = 0, set(), []
    for keyword in keywords:
        args = {"log_groups": groups, "query": _fixed_logs_query(keyword),
                "from_time": start, "to_time": end, "limit": _MAX_LOGS_LIMIT}
        payload = _payload("aws.query_logs", args)
        yield _step("cw_logs_query", "查 CloudWatch Logs · 关键字 %s（%d 个 group）" % (
            keyword, len(groups)), server="cloudwatch", operation="aws.query_logs")
        _record("attempted", "aws.query_logs", payload, keyword=keyword)
        try:
            out = mcp_client.call("aws.query_logs", payload)
        except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
            _record("failed", "aws.query_logs", payload, refused_locally=True)
            packet["not_investigated"].append(f"aws.query_logs is not callable: {exc}.")
            yield _step("unwired", "aws.query_logs 未接线", server="cloudwatch",
                        operation="aws.query_logs", refused_locally=True)
            return
        except mcp_client.TransportError as exc:
            _record("failed", "aws.query_logs", payload, error=str(exc)[:200])
            packet["not_investigated"].append(
                f"aws.query_logs did not respond for {keyword!r} ({str(exc)[:160]}): {_transport_note(exc)} "
                f"That keyword was NOT searched.")
            yield _step("query_failed", f"{keyword} 查询没有响应", server="cloudwatch",
                        operation="aws.query_logs")
            continue

        _record("executed", "aws.query_logs", payload, keyword=keyword,
                elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
        outcome, detail = incident_parse._tool_outcome(out)
        if outcome == "error":
            packet["not_investigated"].append(
                f"aws.query_logs REPORTED AN ERROR for {keyword!r}: "
                f"{redact(str(detail)[:200], counts)}. The tool refusing is not an empty log.")
            yield _step("query_rejected", f"{keyword} 被拒绝", server="cloudwatch",
                        operation="aws.query_logs")
            continue

        body = incident_parse._decode(out.get("text"), out.get("structured"))
        if outcome == "empty" or body in (None, [], {}):
            yield _step("query_empty", f"{keyword}：0 条", server="cloudwatch",
                        operation="aws.query_logs")
            continue

        lines = incident_parse._parse_cloudwatch_log_lines(body)
        if lines is None:
            packet["not_investigated"].append(
                f"aws.query_logs SUCCEEDED for {keyword!r} but our parser could not read the "
                f"response shape ({incident_parse.describe_shape(body)}). OUR wiring gap, NOT an empty log.")
            yield _step("query_unreadable", f"{keyword} 返回体读不懂（我方映射缺口）",
                        server="cloudwatch", operation="aws.query_logs", shape_error=True)
            continue

        matches += len(lines)
        for line in lines:
            classes.update(_EXCEPTION_CLASS.findall(line))
        # Redact FIRST, then bound — the same order as LogDream, so no unredacted text can survive
        # a truncation that happens to cut before the redaction pass.
        for line in lines[:_MAX_EXCERPTS - len(excerpts)]:
            excerpts.append(redact(line[:_MAX_EXCERPT_CHARS], counts))
        yield _step("cw_logs_hit", f"{keyword}：{len(lines)} 条", server="cloudwatch",
                    operation="aws.query_logs")

    if matches:
        packet["evidence"].append({
            "kind": "cloudwatch_logs",
            "environment": "production",
            "source": "cloudwatch",
            "log_groups_seen": len(groups),
            "matches_seen": matches,
            "exception_classes": sorted(classes)[:_MAX_EXCERPTS],
            "excerpts": excerpts[:_MAX_EXCERPTS],
            "keywords": keywords,
            "window_utc": {"start": start, "end": end},
            "reading_rule": ("Only the log groups this alarm's own resource dimension resolved to, "
                             "only these keywords, and only this bounded UTC window were queried. "
                             "A nil result speaks to those and nothing else."),
        })
        packet["contains_production_data"] = True


def _cloudwatch_tags(identity, packet, counts):
    """Ownership labels for the alarm's own resource — only ever from an ARN it actually carried.

    Why this is conditional and usually silent: a tag lookup needs an ARN, and `get_alarm`'s
    Dimensions almost always give a resource NAME. The intranet scanned 500 real alarms and found
    ZERO explicit ARNs, so on today's data this branch makes no call at all. It exists because the
    rule is what matters, not the current hit rate: an ARN that appears IN THE ALARM'S OWN dimension
    is by construction the alarm's target, and anything else is a different resource wearing the
    right-looking name. An ARN assembled from a resource name, or the alarm's own `AlarmArn`, would
    return real tags for the WRONG thing — worse than returning none, because it looks like an answer.
    """
    ledger = packet["cloudwatch_tags"]
    resource = _explicit_resource_identity(identity)
    arn = resource.get("resource_arn") or ""
    if not arn:
        packet["not_investigated"].append(
            "resource tags were not looked up: the alarm's dimensions carry a resource NAME, not an "
            "ARN, and an ARN is never assembled from a name (account, region and resource type are "
            "all missing) nor taken from the alarm's own AlarmArn. This says nothing about whether "
            "the resource has tags or an owner.")
        return

    # The abstract arg is `resource`; the config maps it to their `resourceArn`. Passing
    # `resource_arn` here would silently drop the only required parameter.
    payload = _payload("aws.resource_tags", {"resource": arn})
    yield _step("cw_tags", "查资源标签（告警维度里带的是明确 ARN）",
                server="cloudwatch", operation="aws.resource_tags")
    ledger["attempted"].append({"server": "cloudwatch", "operation": "aws.resource_tags",
                                "args_sent": sorted(payload)})
    try:
        out = mcp_client.call("aws.resource_tags", payload)
    except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
        ledger["failed"].append({"server": "cloudwatch", "operation": "aws.resource_tags",
                                 "args_sent": sorted(payload), "refused_locally": True})
        packet["not_investigated"].append(f"aws.resource_tags is not callable: {exc}.")
        return
    except mcp_client.TransportError as exc:
        ledger["failed"].append({"server": "cloudwatch", "operation": "aws.resource_tags",
                                 "args_sent": sorted(payload), "error": str(exc)[:200]})
        packet["not_investigated"].append(
            f"aws.resource_tags did not respond ({str(exc)[:160]}): {_transport_note(exc)} "
            f"No ownership label was read.")
        return

    ledger["executed"].append({"server": "cloudwatch", "operation": "aws.resource_tags",
                               "args_sent": sorted(payload), "elapsed_ms": out.get("elapsed_ms"),
                               **_transport_meta(out)})
    outcome, detail = incident_parse._tool_outcome(out)
    if outcome == "error":
        packet["not_investigated"].append(
            f"aws.resource_tags REPORTED AN ERROR: {redact(str(detail)[:200], counts)}. Their tool "
            f"refusing is not an absence of tags.")
        return
    body = incident_parse._decode(out.get("text"), out.get("structured"))
    if outcome == "empty" or body in (None, "", [], {}):
        return
    keys = incident_parse._tag_keys(body)
    if keys is None:
        packet["not_investigated"].append(
            f"aws.resource_tags SUCCEEDED but our parser could not read the response shape "
            f"({incident_parse.describe_shape(body)}). OUR wiring gap, NOT an untagged resource.")
        yield _step("query_unreadable", "资源标签返回体读不懂（我方映射缺口）",
                    server="cloudwatch", operation="aws.resource_tags", shape_error=True)
        return

    lowered = {key.lower() for key in keys}
    item = {"kind": "cloudwatch_resource_tags", "environment": "production", "source": "cloudwatch",
            "tag_count": len(keys),
            "note": ("presence of tag KEYS only. Tag values are never read out of the response — "
                     "they carry person names, emails and internal ids. An `owner` tag does NOT "
                     "establish which repository this resource belongs to; there is no confirmed "
                     "mapping from any tag key to a repo.")}
    for marker, field in _TAG_KEYS_OF_INTEREST.items():
        item[field] = item.get(field, False) or marker in lowered
    packet["evidence"].append(item)
    yield _step("cw_tags_done", "资源标签：%d 个 key（只记有无，不记值）" % len(keys),
                server="cloudwatch", operation="aws.resource_tags")


def _relative_to_alarm(stamp_text, window):
    """`before_alarm` / `after_alarm` / `outside_window` / `unknown` — a CATEGORY, never a time."""
    start, end = (window or {}).get("start_utc") or "", (window or {}).get("end_utc") or ""
    text = str(stamp_text or "").strip()
    if not text or not start or not end:
        return "unknown"
    # String comparison is valid here: every stamp on this path is the same normalised UTC format.
    marker = text[:len(start)]
    if marker < start or marker > end:
        return "outside_window"
    alert = (window or {}).get("alert_utc") or ""
    if not alert:
        return "unknown"
    return "before_alarm" if marker <= alert[:len(marker)] else "after_alarm"


def _cloudwatch_context(cw, identity, window, packet, counts):
    """`aws.alarm_history` + `aws.recent_changes`. Categories only; never a root cause."""
    ledger = packet["cloudwatch_history"]

    def _record(bucket, operation, args_sent, **extra):
        ledger[bucket].append({"server": "cloudwatch", "operation": operation,
                               "args_sent": sorted(args_sent), **extra})

    def _run(operation, wanted, label):
        """Attempt one context call. Returns the decoded body or None; records every outcome."""
        payload = _payload(operation, wanted)
        _record("attempted", operation, payload)
        yield _step("cw_context", label, server="cloudwatch", operation=operation)
        try:
            out = mcp_client.call(operation, payload)
        except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
            _record("failed", operation, payload, refused_locally=True)
            packet["not_investigated"].append(f"{operation} is not callable: {exc}.")
            yield _step("unwired", f"{operation} 未接线，未发出请求", server="cloudwatch",
                        operation=operation, refused_locally=True)
            return
        except mcp_client.TransportError as exc:
            _record("failed", operation, payload, error=str(exc)[:200])
            packet["not_investigated"].append(
                f"{operation} did not respond ({str(exc)[:160]}): {_transport_note(exc)} This is CONTEXT "
                f"that is missing, not a finding — the metric result above is unaffected.")
            yield _step("query_failed", f"{operation} 没有响应", server="cloudwatch",
                        operation=operation)
            return
        _record("executed", operation, payload, elapsed_ms=out.get("elapsed_ms"),
                **_transport_meta(out))
        outcome, detail = incident_parse._tool_outcome(out)
        if outcome == "error":
            packet["not_investigated"].append(
                f"{operation} REPORTED AN ERROR: {redact(str(detail)[:200], counts)}. Their tool "
                f"refusing is not an absence of history or changes.")
            yield _step("query_rejected", f"{operation} 被拒绝", server="cloudwatch",
                        operation=operation)
            return
        yield ("body", incident_parse._decode(out.get("text"), out.get("structured")), out)

    # --- alarm history: has this alarm been flapping, or did it just start? ---------------------
    body = None
    for event in _run("aws.alarm_history",
                      {"alarm_name": cw.get("alarm_name"),
                       "from_time": (window or {}).get("start_utc"),
                       "to_time": (window or {}).get("end_utc"),
                       "history_item_type": "StateUpdate",
                       "max_results": _MAX_CONTEXT_ITEMS},
                      "取告警历史：这次是刚开始还是一直在抖"):
        if isinstance(event, tuple) and event[0] == "body":
            body = event[1]
        else:
            yield event
    if body not in (None, [], {}):
        rows = body if isinstance(body, list) else incident_parse._rows(body, ("items", "history", "entries")) or []
        rows = [row for row in rows if isinstance(row, dict)][:_MAX_CONTEXT_ITEMS]
        stamps = [next((str(v) for k, v in row.items() if "time" in str(k).lower()), "")
                  for row in rows]
        transitions = sum(1 for row in rows
                          if "state" in " ".join(str(k).lower() for k in row))
        packet["evidence"].append({
            "kind": "cloudwatch_alarm_history", "environment": "production", "source": "cloudwatch",
            "history_items_seen": len(rows),
            "state_transition_count": transitions,
            "first_event_relation": _relative_to_alarm(stamps[0] if stamps else "", window),
            "last_event_relation": _relative_to_alarm(stamps[-1] if stamps else "", window),
            "note": ("counts and relative position only; no history text, ARN, action target or "
                     "raw timeline is kept. Flapping is a shape, not a cause."),
        })
        yield _step("cw_history", "告警历史：%d 条，%d 次状态变化" % (len(rows), transitions),
                    server="cloudwatch", operation="aws.alarm_history")

    # --- recent changes: did anything change just before this fired? ---------------------------
    # `resource` is attached ONLY when get_alarm's own Dimensions carried an explicit resource
    # dimension. Never assembled from the alarm name or a repo id — a wrong resource silently
    # returns another service's change history under this incident's heading.
    wanted = {"alarm_name": cw.get("alarm_name"),
              "from_time": (window or {}).get("start_utc"),
              "to_time": (window or {}).get("end_utc"),
              "max_results": _MAX_CONTEXT_ITEMS}
    resource = incident_plan._explicit_resource(identity)
    if resource:
        wanted["resource"] = resource
    body = None
    for event in _run("aws.recent_changes", wanted,
                      "查告警前后的变更（部署/配置）" + ("" if resource else "：无明确资源维度，只按告警名")):
        if isinstance(event, tuple) and event[0] == "body":
            body = event[1]
        else:
            yield event
    if body not in (None, [], {}):
        rows = body if isinstance(body, list) else incident_parse._rows(body, ("items", "events", "changes")) or []
        rows = [row for row in rows if isinstance(row, dict)][:_MAX_CONTEXT_ITEMS]
        kinds = sorted({str(next((v for k, v in row.items()
                                  if str(k).lower() in ("eventname", "event_name", "kind", "type")),
                                 "unknown"))[:60] for row in rows})
        stamps = [next((str(v) for k, v in row.items() if "time" in str(k).lower()), "")
                  for row in rows]
        relations = [_relative_to_alarm(stamp, window) for stamp in stamps]
        packet["evidence"].append({
            "kind": "cloudwatch_recent_changes", "environment": "production", "source": "cloudwatch",
            "change_seen": bool(rows),
            "change_count": len(rows),
            "change_kinds": kinds[:_MAX_CONTEXT_ITEMS],
            "resource_scoped": bool(resource),
            "nearest_change_relation": ("before_alarm" if "before_alarm" in relations
                                        else (relations[0] if relations else "unknown")),
            "note": ("A change before an alarm is a CO-OCCURRENCE, not a cause. Say a change was "
                     "seen in the window; do not write it up as the reason."
                     + ("" if resource else " No explicit resource dimension was available, so this "
                                            "is scoped by alarm name only and may be broader than "
                                            "this service.")),
        })
        yield _step("cw_changes", "变更记录：%d 条（%s）" % (
            len(rows), "按资源" if resource else "仅按告警名"),
            server="cloudwatch", operation="aws.recent_changes")


def investigate_events(alert_text, repos=None, timezone=None, query_plan=None, keywords=None,
                        sources=None, max_queries=None, owner="", alert_time=None,
                        alarm_name=None, target_repos=None, tracking_id=None,
                        portal_channel="auto"):
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
    query_plan = query_plan or incident_plan.plan(alert_text, repos=repos, timezone=timezone,
                                    keywords=keywords, sources=sources, alert_time=alert_time,
                                    alarm_name=alarm_name, target_repos=target_repos,
                                    tracking_id=tracking_id, portal_channel=portal_channel)
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
        # Alarm history / recent changes are accounted apart from the metric read: they are context
        # about the alarm, not a measurement, and one failing must not make the metric look failed.
        "cloudwatch_history": {"attempted": [], "executed": [], "failed": []},
        # The Portal delivery-record branch, same three-way accounting for the same reason.
        "portal_queries": {"attempted": [], "executed": [], "failed": []},
        # CloudWatch Logs is a SECOND log source, kept out of both `cloudwatch_queries` (a metric
        # ledger) and the LogDream ledger — otherwise "which log chain failed" is unanswerable.
        "cloudwatch_logs": {"attempted": [], "executed": [], "failed": []},
        # Ownership labels. Usually empty — it only runs when the alarm itself carried an ARN.
        "cloudwatch_tags": {"attempted": [], "executed": [], "failed": []},
        "not_investigated": [],
        "contains_production_data": False,
        "environments": {
            # Built from the configured names: the last hand-written copy said `hk1` for `hkl`.
            "logs": "production (LogDream %s — all production, different content)"
                    % " + ".join(incident_plan.log_sources()),
            "metrics": "production (CloudWatch, queried in UTC around the alert time)",
            "route_snapshot": "dev/SCT — absence there is NOT evidence of absence in production",
        },
        "caveats": [],
    }
    if not query_plan.get("any_runnable", query_plan.get("ok")):
        # Neither branch is runnable. Zero MCP calls from here — the refusals ARE the answer, and
        # every one of them is a question for the user rather than something to work around.
        packet["not_investigated"] = (list(query_plan.get("refusals") or [])
                                      + list((query_plan.get("cloudwatch") or {}).get("refusals") or [])
                                      + list((query_plan.get("portal") or {}).get("refusals") or []))
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

    # ---- Portal first, then CloudWatch, then LogDream. THREE independent branches, in ascending
    # order of how much they need to know: Portal needs only a tracking id, CloudWatch needs an
    # alarm name and a window, LogDream needs a resolved app name on top of that. Running the
    # cheapest-to-qualify first means an alert that carries only a tracking id — the largest family
    # here — still gets an answer instead of a refusal. None of the three may block another.
    for event in _portal_branch(query_plan, packet, counts):
        yield event
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
        outcome, text = incident_parse._tool_outcome(listing)
        if outcome == "error":
            # The tool ran and refused — e.g. an unknown source name. Its error body is non-empty, so
            # without this branch it would be split on whitespace and become "app names".
            packet["not_investigated"].append(
                f"source {source!r} was REJECTED by LogDream ({redact(text[:200], counts)}). Nothing was searched "
                f"there. If the name is wrong, fix `servers.logdream.sources` in the intranet's "
                f"mcp_tools.json — a bad source name otherwise costs half the log coverage silently.")
            yield _step("apps_failed", "%s 被服务器拒绝，该 source 不查（可能是 source 名写错）" % source,
                        server="logdream", operation="log.list_apps", source=source,
                        rejected=True, elapsed_ms=listing.get("elapsed_ms"), **_transport_meta(listing))
            continue
        names, note, error = incident_parse.extract_app_names(text, listing.get("structured"))
        if names is None:
            # The listing succeeded but we cannot read its shape. Treated exactly like a source that
            # refused: no app on this source is verified, so nothing on it is queried. The old code
            # split the body on punctuation here and turned `entries`/`entry_type`/`README.txt` into
            # app names — a fabricated app verifies a candidate that does not exist, the read comes
            # back empty, and empty reads as "no problem".
            packet["not_investigated"].append(f"source {source!r}: {error} Nothing was searched there.")
            yield _step("apps_failed", "%s 的应用清单格式看不懂，该 source 不查（不猜应用名）" % source,
                        server="logdream", operation="log.list_apps", source=source,
                        shape_error=True, elapsed_ms=listing.get("elapsed_ms"), **_transport_meta(listing))
            continue
        apps_by_source[source] = set(names)
        if note:
            packet["not_investigated"].append(f"source {source!r} app listing: {note}.")
        yield _step("apps_done", "%s 上有 %d 个应用" % (source, len(names)),
                    server="logdream", operation="log.list_apps", source=source,
                    app_count=len(names), note=note or None,
                    elapsed_ms=listing.get("elapsed_ms"), **_transport_meta(listing))

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
            outcome, text = incident_parse._tool_outcome(found)
            if outcome == "error":
                packet["not_investigated"].append(
                    f"{match}/{source}: the file-search tool REPORTED AN ERROR ({redact(text[:200], counts)}). "
                    f"Nothing was read.")
                yield _step("query_rejected", "%s / %s：文件搜索工具报错" % (match, source),
                            server="logdream", operation="log.search_files",
                            app=match, source=source, rejected=True,
                            elapsed_ms=found.get("elapsed_ms"), **_transport_meta(found))
                continue
            picked = incident_parse.select_log_files(text, alert_date=alert_date,
                                      structured=found.get("structured"))
            if not picked:
                packet["not_investigated"].append(
                    f"{match}/{source}: the file search returned no recognisable log file, so "
                    f"nothing was read. A file name is never guessed.")
                yield _step("no_files", "%s / %s：没找到可用的日志文件，不读" % (match, source),
                            server="logdream", operation="log.search_files",
                            app=match, source=source, elapsed_ms=found.get("elapsed_ms"), **_transport_meta(found))
                continue
            files_by_source[source] = picked
            target.setdefault("files", {})[source] = picked
            yield _step("files_found", "%s / %s：选中 %s" % (match, source, "、".join(picked)),
                        server="logdream", operation="log.search_files",
                        app=match, source=source, files=picked,
                        elapsed_ms=found.get("elapsed_ms"), **_transport_meta(found))

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
            outcome, text = incident_parse._tool_outcome(out)
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
                            elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
                continue
            if outcome == "empty":
                yield _step("query_empty", "%s / %s / %s：%s 无匹配" % (
                    match, source, log_file, term),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term, file=log_file,
                            elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
                continue
            # Structured body -> the actual log-line fields. Splitting the JSON source counted 11
            # "lines" for a 2-line response (intranet, 2026-07-31), and every number downstream —
            # lines_seen, exception classes, excerpts, the retained raw — was computed off that.
            lines, reported, shape_error = incident_parse.extract_log_lines(text, out.get("structured"))
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
                            shape_error=True, elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
                continue
            if not lines:
                yield _step("query_empty", "%s / %s / %s：%s 无匹配" % (
                    match, source, log_file, term),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term, file=log_file,
                            elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out))
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
                        elapsed_ms=out.get("elapsed_ms"), **_transport_meta(out),
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
