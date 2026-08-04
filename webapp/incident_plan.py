"""Alert text -> a read-only query plan. Opens no sockets; fully testable offline.

Separated from the investigation because the two fail in completely different ways. A planning bug
means we would have asked the wrong question; an investigation bug means we mishandled the answer.
Keeping them apart means the planning half — which is where the timezone, app-name and window rules
live — can be exercised without a production system anywhere near the test.

The rules that matter most here, all of them paid for:

* **No time defaulting, ever.** CloudWatch writes UTC, LogDream defaults to Asia/Hong_Kong and the
  servers are GMT, so the same clock time can be three moments 8 hours apart. A window missing its
  DATE or its ZONE produces a blocking refusal and zero queries — because searching the wrong window
  returns nothing, and nothing reads as "no problem".
* **Repo -> app names are candidates, never answers.** RUNBOOK-55: 0% are identical and ~36% resolve
  by rule, so every candidate is checked against the server's own listing before anything is read.
* **Names come from the intranet where they can.** Log sources, the app map and the `alert_time`
  wire format are all config-first, built-in second; hard-coding any of them is the mistake
  RUNBOOK-49/50/51 were three separate fixes for.
"""
import json
import math
import os
import re
from datetime import datetime, timedelta
from datetime import timezone as _utc_tz          # `timezone` is a parameter name all over this file

from retriever import code as rcode, incident, messages as msg, repo_tags
from . import mcp_registry


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
# The ONLY dimension names that identify a resource well enough to scope a change lookup. Anything
# else (and emphatically the alarm name or a repo id) is not a resource — see the intranet handoff
# §3.4: a guessed resource returns another service's change history under this incident's heading.
_RESOURCE_DIMENSIONS = ("ServiceName", "DBClusterIdentifier", "DBInstanceIdentifier", "QueueName",
                        "FunctionName", "LoadBalancer", "TargetGroup", "ClusterName",
                        "Cluster Name", "TableName", "StreamName")


def _explicit_resource(identity):
    """A resource name taken VERBATIM from an explicit alarm dimension, or "" — never assembled."""
    dimensions = (identity or {}).get("dimensions") or []
    if not isinstance(dimensions, list):
        return ""
    # `alarm_metric_identity` keeps dimensions as [{"Name": ..., "Value": ...}] in the alarm's own
    # order. Preference follows _RESOURCE_DIMENSIONS, not that order, so the same alarm always
    # scopes to the same resource.
    by_name = {str(item.get("Name") or ""): str(item.get("Value") or "")
               for item in dimensions if isinstance(item, dict)}
    for name in _RESOURCE_DIMENSIONS:
        value = by_name.get(name, "").strip()
        if value:
            return value
    return ""

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
_MAX_KEYWORDS = 8


# ---- the query plan -------------------------------------------------------------------------

# ---- how far LogDream may be reached at all -----------------------------------------------------
# The intranet's 2026-08-04 audit: today only ONE app is in scope (`portal` on `hkp3`), but the
# candidate rule still let 35 unrelated repos match a name in that source's 92-app listing. Passing
# the live-listing check is not the same as being in scope — the listing says an app EXISTS, not
# that we are allowed to read it — so scope is now a separate gate that runs first.
#
# Both knobs live in `servers.logdream` in the intranet's config, and both DEFAULT TO OPEN: an
# absent knob must behave exactly as before it existed, or pulling this change would silently narrow
# a deployment nobody meant to narrow.
SCOPE_MAPPING_ONLY = "explicit_mapping_only"
SCOPE_MAPPING_THEN_RULES = "mapping_then_rules"

# Which files are worth reading, in preference order. Config-first for the same reason every other
# name here is: it is THEIR filesystem. `sftp.log` was in the built-in list from RUNBOOK-55 and the
# app now in scope does not have one (owner, 2026-08-04) — a file that does not exist costs a query
# out of the budget and comes back as a rejection, and a rejection read as "no lines" reads as
# "no problem".
DEFAULT_LOG_FILES = ("otx_trace.log", "exception.log")


def preferred_log_files():
    """Log file names to try, from `servers.logdream.log_files` in the intranet config.

    Reads the named roles (`trace`, `exception`) plus anything under `other`, so the box can add a
    file without this side knowing what it is called.
    """
    declared = (mcp_registry.servers().get("logdream") or {}).get("log_files")
    if not isinstance(declared, dict):
        return DEFAULT_LOG_FILES
    names = []
    for key in ("trace", "exception"):
        value = declared.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "?":
            names.append(value.strip())
    other = declared.get("other")
    if isinstance(other, (list, tuple)):
        names.extend(str(name).strip() for name in other
                     if str(name).strip() and str(name).strip() != "?")
    # Order preserved, duplicates dropped. An empty/unfilled config falls back rather than
    # producing a plan with nothing to read.
    seen, unique = set(), []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    return tuple(unique) or DEFAULT_LOG_FILES


def logdream_scope():
    """`{allowed_apps, allowed_sources, policy}` — the hard scope, from the intranet's config.

    `allowed_apps` empty means no app restriction. `policy` of `explicit_mapping_only` means a repo
    with no entry in the intranet's app map yields NOTHING: the naming rule is a good guess and a
    good guess is exactly what must not reach production when the scope is one named app.
    """
    spec = mcp_registry.servers().get("logdream") or {}
    declared = spec.get("allowed_apps")
    allowed = tuple(str(name).strip() for name in declared
                    if str(name).strip()) if isinstance(declared, (list, tuple)) else ()
    policy = str(spec.get("app_resolution_policy") or "").strip().lower()
    return {
        "allowed_apps": allowed,
        # Which sources may be touched AT ALL, as opposed to which are queried by default. A
        # drill-down (`sources=[...]`) is a caller-supplied value and must not be able to reach a
        # source the intranet switched off.
        "allowed_sources": tuple(log_sources()),
        "policy": policy if policy in (SCOPE_MAPPING_ONLY, SCOPE_MAPPING_THEN_RULES)
        else SCOPE_MAPPING_THEN_RULES,
    }


def app_candidates(repo):
    """Repo id -> candidate LogDream app names, with how each was derived.

    RUNBOOK-55: 0% are identical, ~36% resolve by rule. So these are candidates to be checked
    against the server's own app list, never answers. An intranet-owned mapping wins when present;
    absent, the built-in rule applies — the box owns that file and cannot push it here.

    Scope is applied HERE rather than at the query, so a repo outside it produces no candidates at
    all and the plan says so, instead of producing a candidate that then quietly fails a check
    further down where the reason is harder to read.
    """
    repo = (repo or "").strip()
    if not repo:
        return []
    scope = logdream_scope()
    allowed = {name.lower() for name in scope["allowed_apps"]}

    def _in_scope(entries):
        return [entry for entry in entries if not allowed or entry["app"].lower() in allowed]

    mapped = _app_map().get(repo.lower())
    if mapped:
        return _in_scope([{"app": mapped,
                           "how": "config/logdream_apps.json (intranet-owned mapping)",
                           "confidence": "confirmed"}])
    if scope["policy"] == SCOPE_MAPPING_ONLY:
        # No mapping, and the intranet has said mappings are the only way in. The naming rule is
        # not consulted: its whole job is to guess, and a guess that happens to name a real app in
        # the listing would read as a confirmed target.
        return []
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
    return _in_scope(out)


def runnable_log_targets(query_plan):
    """The targets the LogDream branch can actually query: those with at least one app candidate.

    ONE definition, used by the planner to decide `ok` and by the investigator to decide whether to
    open a socket. Two copies of this predicate would be two things that can drift, and the shape of
    that drift is the defect this exists to close: the planner says the branch is runnable, the
    investigator agrees just long enough to make one metadata call, and then discovers per target
    that there was never anything to query.

    Takes a plan rather than reading module state on purpose — `investigate_events` accepts a
    caller-supplied `query_plan`, so the investigator has to be able to run this on a plan it did
    not build and does not trust.
    """
    return [target for target in (query_plan or {}).get("targets") or []
            if isinstance(target, dict) and target.get("app_candidates")]


def scope_refusal(repo):
    """Why `app_candidates` came back empty, in words a reader can act on — or "" if it did not.

    The distinction that has to survive: "we could not work out this repo's app name" and "this repo
    is outside the scope somebody deliberately set" are different situations. The first is a gap to
    close; the second is the system working. Both produce zero candidates.
    """
    repo = (repo or "").strip()
    if not repo or app_candidates(repo):
        return ""
    scope = logdream_scope()
    mapped = _app_map().get(repo.lower())
    if mapped and scope["allowed_apps"]:
        return (f"{repo} maps to LogDream app {mapped!r}, which is OUTSIDE the configured scope "
                f"({', '.join(scope['allowed_apps'])}). It was not queried. This is a deliberate "
                f"restriction in the intranet's config, NOT a missing mapping and NOT an empty log.")
    if scope["policy"] == SCOPE_MAPPING_ONLY:
        return (f"{repo} has no entry in the intranet's LogDream app map, and the configured policy "
                f"is {SCOPE_MAPPING_ONLY!r} — so no app name was guessed from the repo name and "
                f"nothing was queried. Add the mapping to config/logdream_app_map.json to include "
                f"it. A guessed name that happens to exist on the server would read as a confirmed "
                f"target, which is why it is refused instead.")
    return ""


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
         alarm_name=None, target_repos=None, tracking_id=None, portal_channel="auto"):
    """Alert text -> a read-only query plan. Opens no sockets; fully testable offline.

    `keywords` and `sources` are the drill-down path: a follow-up question ("search for
    ConnectException instead", "only hkp3") re-runs with them instead of the derived list, so
    narrowing does not mean starting over. Caller-supplied keywords are marked as such, because the
    provenance of a keyword is what separates our query plan from "grep for ERROR".

    `target_repos` is WHERE to look, and it exists because the alert text is not the only place that
    knowledge lives. The caller has usually already run `incident_impact`, or the user simply named
    the service — and the largest alert family here ("MDC Alert - General SHP API Error") names no
    repo at all, so without this the investigator refuses and asks a question the caller could
    already answer. Supplied ids are validated against the same repo universe the text scan uses and
    carry `source: "supplied by the caller"` into the packet: a nil result on a repo somebody named
    means less than a nil result on one the alert itself identified.

    `repos` is a different thing entirely — the repo UNIVERSE to scan the text against (injectable
    because index/repo_tags.json is gitignored). Do not confuse the two.
    """
    parsed = incident.parse_alert(alert_text, repos=repos)
    out = {
        "ok": False,
        "parsed": parsed,
        "targets": [],
        "keywords": [],
        "window": None,
        "sources": [],       # filled in below, after the caller's request meets the scope gate
        "log_files": list(preferred_log_files()),
        "refusals": [],
        # The CloudWatch half. Kept separate so either branch can run when the other cannot: an
        # unresolvable alarm name must not block a log investigation that is ready to go, and an
        # unidentifiable repo must not block a metric lookup that only needs the alarm name.
        "cloudwatch": {"runnable": False, "alarm_name": "", "alarm_name_source": "",
                       "refusals": []},
        # The Portal half. `MDC Alert - General SHP API Error` is one of the largest alert families
        # here and it is NEITHER a CloudWatch alarm NOR resolvable to a LogDream app — its only
        # investigation path is a tracking id against Portal's delivery record. Kept as a THIRD
        # independent branch on purpose: it needs no repo and no time window, so the log branch's
        # window gate must not be able to block it.
        "portal": {"runnable": False, "tracking_id": "", "tracking_id_source": "",
                   "channel": "auto", "refusals": []},
    }
    # A caller-supplied `sources` is a drill-down, not an override. It may NARROW the configured set
    # and nothing else: a source the intranet switched off (`query_by_default: false`) is off, and a
    # name the model invented is not a source at all. Before this, both went straight to the wire.
    allowed_sources = list(log_sources())
    asked = [str(name).strip() for name in (sources or ()) if str(name).strip()]
    out_of_scope = [name for name in asked if name not in allowed_sources]
    out["sources"] = [name for name in asked if name in allowed_sources] or allowed_sources
    if out_of_scope:
        out["refusals"].append(
            "these log sources were requested but are not in the configured set, so they were NOT "
            "queried: %s. The configured sources are %s. A source that is switched off or "
            "misspelled returns a rejection, and a rejection read as 'no lines' reads as "
            "'no problem'." % (", ".join(sorted(out_of_scope)), ", ".join(allowed_sources)))
    if out["sources"] != allowed_sources:
        # Built from the configured source names, never a literal: the last time these were spelled
        # out by hand the text said `hk1` where the server accepts `hkl`.
        out["sources_note"] = (
            "sources narrowed by the caller. %s are ALL production with different content, so a "
            "single-source result covers less than the default — say which one was searched."
            % " and ".join(allowed_sources))
    identified = bool(parsed["identified"])

    # Repos the CALLER already knows are involved. Checked against the universe the text scan uses:
    # only reject when there IS a universe to check against — when index/repo_tags.json is missing
    # the caller is a better source than our absent index, so the id is accepted and marked
    # unvalidated rather than refused for a gap on our side.
    universe = set(incident.known_repos(repos))
    supplied_repos, unknown_repos = [], []
    for name in target_repos or []:
        name = str(name).strip()
        if not name:
            continue
        if universe and name not in universe:
            unknown_repos.append(name)
        elif name not in supplied_repos:
            supplied_repos.append(name)

    text_repos = [entry["repo"] for entry in parsed["repos"]] if identified else []
    target_ids = text_repos + [name for name in supplied_repos if name not in text_repos]

    if unknown_repos:
        out["refusals"].append(
            "these repo ids were supplied by the caller but are not in the repo universe, so they "
            "were NOT queried: %s. Check the id (they look like `mc-hk-hase-...`) — this is a "
            "wrong name, not an empty log." % ", ".join(sorted(unknown_repos)))
    if not target_ids and not parsed["use_cases"]:
        out["refusals"].append(
            "no repo and no known use-case id could be read from this alert, and none was supplied, "
            "so there is nothing to query. Ask for the service name or the use-case id, or pass "
            "`repos` if you already know which service it is; do not guess an app.")
    if supplied_repos:
        out["targets_note"] = (
            "%d of these targets were supplied by the caller rather than read from the alert text. "
            "A nil result on a caller-named repo only says that repo's logs were clean for these "
            "terms — it does not confirm the caller named the right service." % len(supplied_repos))

    seen_keywords = {}

    def _add_keyword(term, why):
        term = (term or "").strip()
        if term and term.lower() not in seen_keywords:
            seen_keywords[term.lower()] = {"term": term, "why": why}

    for repo in target_ids:
        target = {"repo": repo,
                  "source": ("named in the alert text" if repo in text_repos
                             else "supplied by the caller"),
                  "app_candidates": app_candidates(repo),
                  "app_resolved": "", "app_note": ""}
        if universe and repo in supplied_repos:
            target["validated"] = True
        elif repo in supplied_repos:
            target["validated"] = False
            target["app_note"] = ("repo universe unavailable (index/repo_tags.json missing), so "
                                  "this caller-supplied id could not be confirmed to exist")
        if not target["app_candidates"]:
            # "outside a scope somebody set" and "we could not work out the name" are different
            # situations that both produce zero candidates. The first is the system working; the
            # second is a gap to close. Reporting them as one would make a deliberate restriction
            # look like a defect, and a defect look like a decision.
            refusal = scope_refusal(repo)
            note = refusal or ("no LogDream app name could be derived from this repo id; "
                               "an intranet mapping is needed (config/logdream_apps.json)")
            # APPENDED, not assigned. `app_note` may already say the repo universe was unavailable
            # so this id could not be confirmed to exist — a different fact from the scope one, and
            # overwriting it would drop the reason the caller most needs to hear.
            target["app_note"] = f"{target['app_note']}; {note}" if target["app_note"] else note
            target["out_of_scope"] = bool(refusal)
            if refusal:
                out["refusals"].append(refusal)
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

    # ---- the Portal half: a tracking id, and nothing else -------------------------------------
    # Priority is the same shape as the alarm name: what the caller said, then what the alert text
    # uniquely contains, then nothing. Never a guess — a wrong tracking id returns ANOTHER
    # customer's delivery record, which is worse than returning none.
    portal = out["portal"]
    channel = (portal_channel or "auto").strip().lower()
    if channel not in ("sms", "email", "auto"):
        portal["refusals"].append(
            f"portal_channel {channel!r} is not one of sms/email/auto, so the Portal branch was "
            f"skipped rather than defaulting to something you did not ask for.")
        channel = ""
    portal["channel"] = channel or "auto"

    supplied_tracking = incident.valid_tracking_id(tracking_id) if tracking_id else ""
    if tracking_id and not supplied_tracking:
        portal["refusals"].append(
            "the supplied tracking_id is not usable (empty, multi-line, out of length bounds, or "
            "contains characters a tracking id cannot have), so no Portal call was made.")
    if supplied_tracking:
        portal["tracking_id"] = supplied_tracking
        portal["tracking_id_source"] = "supplied by the caller"
    else:
        extracted = incident.extract_tracking_id(alert_text)
        if extracted:
            portal["tracking_id"] = extracted
            portal["tracking_id_source"] = (
                "extracted from a labelled line in the alert text (trackId/trackingId/tracking_id)")
        elif not tracking_id:
            portal["refusals"].append(
                "no single labelled tracking id could be read from this alert, so the Portal "
                "delivery-record branch was skipped. Pass `tracking_id` if you have one. It is NOT "
                "guessed from phone numbers, message references or payload UUIDs — a wrong id "
                "returns a different customer's record.")
    portal["runnable"] = bool(portal["tracking_id"] and channel)

    # Runnable, not merely "we understood the alert". Identifying the service is necessary and not
    # sufficient: this tool's real parameter is an ALERT TIME it backtracks from, so without a
    # window there is no honest query to send. The plan used to stay ok=True here and the
    # investigation went ahead untimed (intranet, 2026-07-31) — the refusal was reported while the
    # calls were made anyway, which is the worst of both: production reads, and an answer nobody
    # can scope.
    #
    # `targets` is NOT the test, and neither is `use_cases` (intranet, 2026-08-04). An out-of-scope
    # repo still produces a target — kept deliberately, so the UI and the audit trail can show WHY
    # it was refused — but its `app_candidates` are empty and there is nothing to query. Reading a
    # non-empty `targets` as "runnable" meant `log.list_apps` fired against production for a repo
    # the scope gate had already excluded: the gate stopped the file search and the log read, and
    # then leaked one metadata call in front of them.
    #
    # `use_cases` was worse: nothing in this planner ever converts a use case into a repo/app
    # target, so a use-case-only alert opened the log branch with literally nothing for it to do.
    # If use-case -> repo/app resolution is built later, it belongs in `targets` with real
    # candidates, and this line then picks it up for free.
    out["log_targets"] = [target["repo"] for target in runnable_log_targets(out)]
    out["ok"] = bool(out["log_targets"]) and bool(out["window"])
    # ANY branch alone is enough to be worth running. A CloudWatch failure must not break a log
    # investigation that works, the reverse holds too, and Portal must run on a tracking id ALONE —
    # no repo, no alarm, no time window. That last case is the whole point of the branch: the alert
    # family it serves carries none of the other three.
    out["any_runnable"] = bool(out["ok"] or cw["runnable"] or portal["runnable"])
    return out
