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

* **LogDream `hk1`/`hkp3` and CloudWatch are PRODUCTION** (owner confirmed 2026-07-29: both
  LogDream sources are production, holding different logs).
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
  anomaly", which is the worst possible failure for this feature.
"""
import hashlib
import os
import re

from retriever import code as rcode, incident, messages as msg, repo_tags
from . import config, incident_raw_store, mcp_client, mcp_registry

# LogDream's two sources are BOTH production, holding different logs, so both are queried and every
# piece of evidence says which one it came from (owner, 2026-07-29).
PRODUCTION_SOURCES = ("hk1", "hkp3")
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
# `incident_raw_store.put()`. Do not add a key here that could carry log-derived text.
_IDENTIFIER_KEYS = frozenset({"raw_ref"})


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
            return f"[removed at exit: matched {', '.join(kinds)}]", report
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


def plan(alert_text, repos=None, timezone=None, keywords=None, sources=None):
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
                   or list(PRODUCTION_SOURCES),
        "log_files": ["otx_trace.log", "exception.log"],
        "refusals": [],
    }
    if out["sources"] != list(PRODUCTION_SOURCES):
        out["sources_note"] = ("sources narrowed by the caller. hk1 and hkp3 are BOTH production "
                               "with different content, so a single-source result covers less than "
                               "the default — say which one was searched.")
    if not parsed["identified"]:
        out["refusals"].append(
            "no repo and no known use-case id could be read from this alert, so there is nothing to "
            "query. Ask for the service name or the use-case id; do not guess an app.")
        return out

    seen_keywords = {}

    def _add_keyword(term, why):
        term = (term or "").strip()
        if term and term.lower() not in seen_keywords:
            seen_keywords[term.lower()] = {"term": term, "why": why}

    for entry in parsed["repos"]:
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

    for item in parsed["use_cases"]:
        _add_keyword(item.get("use_case"), "use-case id named in the alert text")

    if parsed.get("metric"):
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

    # The window. Never defaulted: see the module docstring on the three coexisting timezones.
    zoned = [t for t in parsed.get("times") or [] if isinstance(t, dict) and t.get("timezone")]
    if zoned:
        # An explicit zone in the alert beats a caller-supplied one: the alert is the evidence.
        out["window"] = {"at": zoned[0].get("text") or "", "timezone": zoned[0]["timezone"],
                         "source": "explicit in the alert text"}
    elif timezone and (parsed.get("times") or []):
        first = (parsed["times"] or [])[0]
        out["window"] = {"at": first.get("text") or "", "timezone": timezone,
                         "source": "caller-supplied timezone (the alert's own time was ambiguous)"}
    else:
        out["refusals"].append(
            "the alert carries no time with an explicit timezone, so no window was built. "
            "CloudWatch is UTC, LogDream defaults to Asia/Hong_Kong and the servers are GMT — a "
            "guessed window returns nothing and reads as 'no anomaly'. Ask which zone the alert "
            "timestamp is in, or pass timezone= explicitly.")

    out["ok"] = bool(out["targets"] or parsed["use_cases"])
    return out


# ---- the investigation ----------------------------------------------------------------------

def _evidence_from_text(raw, keyword, source, app, log_file, counts, owner="", window=None):
    """One log response -> an aggregate. `raw` is local and stays local.

    When raw retention is on (UAT internal test only) the original lines are handed to
    `incident_raw_store` and the evidence carries an opaque `ref` for the browser to fetch. The raw
    text still does not travel in this dict, so the model's view is unchanged either way.
    """
    lines = [line for line in (raw or "").splitlines() if line.strip()]
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
        "source": source,
        "app": app,
        "file": log_file,
        # LogDream hk1/hkp3 are both production (owner, 2026-07-29). Labelled on every item so a
        # caller never has to infer it from the source name.
        "environment": "production" if source in PRODUCTION_SOURCES else "unknown",
        "matched_keyword": keyword,
        "lines_seen": len(lines),
        "lines_returned": len(excerpts),
        "exception_classes": [name for name, _ in
                              sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))][:6],
        "excerpts": excerpts,
        "excerpt_policy": (f"redacted, first {_MAX_EXCERPTS} matching lines, "
                           f"{_MAX_EXCERPT_CHARS} chars each. The full response was held in memory "
                           f"and discarded; it is not retrievable from this packet."),
    }


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


def investigate_events(alert_text, repos=None, timezone=None, query_plan=None, keywords=None,
                        sources=None, max_queries=None, owner=""):
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
                                    keywords=keywords, sources=sources)
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
        "queries_run": [],
        "not_investigated": [],
        "contains_production_data": False,
        "environments": {
            "logs": "production (LogDream hk1 + hkp3, both production, different content)",
            "route_snapshot": "dev/SCT — absence there is NOT evidence of absence in production",
        },
        "caveats": [],
    }
    if not query_plan.get("ok"):
        packet["not_investigated"] = list(query_plan.get("refusals") or [])
        packet["caveats"].append("nothing was queried; see not_investigated")
        yield _step("refused", "拒绝调查：告警里读不出服务或用例，不猜", reasons=packet["not_investigated"])
        yield {"type": "result", "packet": _finish(packet, counts)}
        return

    if not config.MCP_ENABLED:
        packet["not_investigated"].append(
            "log querying is switched off (SDLC_MCP_ENABLED unset), so this answer rests on local "
            "artefacts only. The blast-radius answer does not need it; a root-cause answer does.")
        packet["caveats"].append("no production data was read")
        yield _step("disabled", "日志查询开关未开（SDLC_MCP_ENABLED），没有读任何生产数据")
        yield {"type": "result", "packet": _finish(packet, counts)}
        return

    # Their own app list is the only authority on app names — ours are candidates (RUNBOOK-55).
    yield _step("apps", "取应用清单（我们推的名字只是候选，以服务器为准）",
                server="logdream", operation="log.list_apps")
    live_apps = set()
    try:
        listing = mcp_client.call("log.list_apps")
        live_apps = {token for token in re.split(r"[\s,\[\]\"']+", listing.get("text") or "")
                     if token}
    except (mcp_client.Disabled, mcp_client.TransportError,
            mcp_registry.NotAllowed, mcp_registry.NotWired) as exc:
        packet["not_investigated"].append(f"could not list LogDream apps: {exc}")
        packet["caveats"].append(
            "app names could not be verified against the server, so no log query was attempted — "
            "querying a guessed app name returns an empty result that reads like 'no problem'.")
        yield _step("apps_failed", "取不到应用清单，因此一条日志都没查（拿猜的名字去查会返回空，"
                                   "而空会被读成“没问题”）",
                    server="logdream", operation="log.list_apps", error=str(exc))
        yield {"type": "result", "packet": _finish(packet, counts)}
        return
    yield _step("apps_done", "服务器上有 %d 个应用" % len(live_apps),
                server="logdream", operation="log.list_apps", app_count=len(live_apps),
                elapsed_ms=listing.get("elapsed_ms"))

    for target in query_plan["targets"]:
        match = next((c["app"] for c in target["app_candidates"] if c["app"] in live_apps), "")
        if not match:
            target["app_note"] = (
                "none of the candidate app names exist on the server: "
                + ", ".join(c["app"] for c in target["app_candidates"])
                + ". Not queried. This repo needs an entry in the intranet's "
                  "config/logdream_apps.json.")
            packet["not_investigated"].append(f"{target['repo']}: {target['app_note']}")
            yield _step("app_unresolved",
                        "%s：候选应用名在服务器上都不存在，跳过不查" % target["repo"],
                        repo=target["repo"],
                        candidates=[c["app"] for c in target["app_candidates"]])
            continue
        target["app_resolved"] = match
        yield _step("app_resolved", "%s → 应用 %s（已在服务器清单中核对）" % (target["repo"], match),
                    repo=target["repo"], app=match)
        # Built up front and then truncated, so what got SKIPPED is exact rather than inferred from
        # where a loop happened to stop.
        wanted = [(keyword["term"], source) for keyword in query_plan["keywords"]
                  for source in query_plan["sources"]]
        budget_left = max(0, budget - len(packet["queries_run"]))
        running, skipped = wanted[:budget_left], wanted[budget_left:]
        if skipped:
            packet["not_investigated"].append(
                f"{match}: the {budget}-read query budget ran out, so these "
                f"keyword/source pairs were never tried: "
                + ", ".join(f"{term} on {source}" for term, source in skipped)
                + ". Raise SDLC_INCIDENT_MAX_LOG_QUERIES or narrow the keywords — do NOT read this "
                  "as 'those keywords found nothing'.")
            yield _step("budget_spent",
                        "查询预算用完（%d 次），%d 个关键词/source 组合没查" % (budget, len(skipped)),
                        budget=budget,
                        skipped=[f"{term}@{source}" for term, source in skipped])
        for index, (term, source) in enumerate(running, 1):
            yield _step("query", "查 %s / %s：关键词 %s（%d/%d）" % (
                match, source, term, index, len(running)),
                server="logdream", operation="log.read",
                app=match, source=source, keyword=term)
            args = {"app": match, "source": source, "keyword": term}
            packet["queries_run"].append({"app": match, "source": source, "keyword": term})
            window = query_plan.get("window")
            if window and window.get("at"):
                # Passed through as given; this module never converts or defaults a time.
                args["from_time"] = window["at"]
                args["timezone"] = window.get("timezone")
            try:
                out = mcp_client.call("log.read", args)
            except (mcp_registry.NotWired, mcp_registry.NotAllowed) as exc:
                packet["not_investigated"].append(f"log.read unavailable: {exc}")
                packet["caveats"].append("the log read operation is not fully wired yet")
                # Refused by the allow-list / naming seam, NOT a failure to reach the server. Ops
                # need these apart: one is "nobody finished wiring this", the other is "the log
                # service is down", and they get escalated to different people.
                yield _step("unwired", "日志读取操作还没接通完（被本地白名单/命名层拒绝，未发出请求）：%s"
                            % exc, server="logdream", operation="log.read", refused_locally=True)
                yield {"type": "result", "packet": _finish(packet, counts)}
                return
            except mcp_client.TransportError as exc:
                packet["not_investigated"].append(
                    f"{match}/{source} keyword {term!r}: log service did not respond ({exc}). "
                    f"This is NOT evidence of no matching lines.")
                yield _step("query_failed",
                            "%s / %s 没响应 —— 这不等于“没有匹配的日志”" % (match, source),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term)
                continue
            if not (out.get("text") or "").strip():
                yield _step("query_empty", "%s / %s：%s 无匹配" % (match, source, term),
                            server="logdream", operation="log.read",
                            app=match, source=source, keyword=term,
                            elapsed_ms=out.get("elapsed_ms"))
                continue
            item = _evidence_from_text(out["text"], term, source, match, "otx_trace.log", counts,
                                       owner=owner, window=query_plan.get("window"))
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
    if not packet["evidence"] and packet["queries_run"] and not packet["not_investigated"]:
        packet["caveats"].append(
            "the queries ran and matched nothing. That is a real finding only for the keywords and "
            "window actually used — see `queries_run`.")
    final = _finish(packet, counts)
    yield _step("summary", "调查完成：%d 条证据，%d 项没查（脱敏 %d 处）" % (
        len(final.get("evidence") or []), len(final.get("not_investigated") or []),
        sum((final.get("redactions") or {}).values())),
        evidence=len(final.get("evidence") or []),
        not_investigated=len(final.get("not_investigated") or []),
        redactions=final.get("redactions") or {})
    yield {"type": "result", "packet": final}


def _finish(packet, counts):
    """Exit gate: defence 2, plus the accounting that makes the wall auditable."""
    packet["redactions"] = dict(sorted(counts.items()))
    cleaned, report = sanitize_packet(packet)
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
