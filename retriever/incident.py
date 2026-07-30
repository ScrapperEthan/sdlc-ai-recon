"""Incident entry point: alert TEXT -> affected repos -> topics -> use cases -> who to notify.

This module deliberately touches **no production system at all** — no MCP, no logs, no AWS. It
answers the half of an incident that the ops tooling cannot answer ("who is affected, who should
be told") purely from artefacts we already build: the repo tag index, the message-edge snapshot and
the use-case routing snapshot. That independence is the point: it works before any MCP permission,
transport or fixture exists, and it keeps working if that integration is ever taken away.

**How a repo is identified — not by parsing the alarm name.** RUNBOOK-55 measured 466/500 (93.2%)
of real CloudWatch alarms as containing a *complete* repo id verbatim, so the reliable move is to
scan the alert text for known repo ids rather than to reverse-engineer a naming convention that AWS
can change under us. Everything else the parser extracts (environment, resource, severity, metric,
timezone) is commentary for the human — it never gates identification, and a token this module has
never seen degrades that one field to empty instead of failing the parse.

Fail-closed, as everywhere else in this project: an alert whose repo cannot be identified returns
``identified: false`` with the reason, never a guess.
"""
import json
import re

from . import config, messages, repo_tags, usecase_catalog

# Built-in fallbacks so a missing/rubbish config/alarm_patterns.json degrades to "fewer commentary
# fields", never to a crash. The committed knob file is authoritative when present.
_DEFAULT_ENVIRONMENTS = ("preproc", "prod", "uat", "sit", "dev")
_DEFAULT_RESOURCE_TYPES = ("ECS", "RDS", "DynamoDB", "SQS", "Lambda", "ALB", "MSK", "sidecar")
_DEFAULT_SEVERITIES = ("CRITICAL", "MAJOR", "MINOR", "WARNING", "WARN", "INFO")
_DEFAULT_TZ_ALIASES = {"HKT": "Asia/Hong_Kong", "HK": "Asia/Hong_Kong", "CST": "Asia/Hong_Kong",
                       "UTC": "UTC", "GMT": "UTC", "Z": "UTC"}

# A repo id is only "in" the text when neither neighbour is alphanumeric. '-' and '_' are allowed
# neighbours on purpose: repo ids contain '-', and the real alarm format wraps them in '_'
# (prodECS_<repo>_service_...). Prefix collisions that this admits (repo 'a-b' inside 'a-b-job')
# are removed afterwards by _drop_subsumed, which keeps the longest match at a position.
_ALNUM = re.compile(r"[A-Za-z0-9]")
_THRESHOLD = re.compile(r"\[([^\]]{1,40})\]")
# "WPB Servicing Realtime High Risk Path" — a capitalised phrase ending in 'Path'. Reported as-is;
# it is NOT joined to anything until the delivery_path name->id map arrives (RUNBOOK-56 Q3).
_PATH_PHRASE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+){0,7}[ _-]Path)\b")
_DATETIME = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)\s*([A-Za-z]{1,4})?")
_CLOCK = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\s*([A-Za-z]{2,4})?")
# Use-case ids (C9508 / M2101 / I0141 / N0278). The second entry point into an incident: the
# biggest alert family carries no repo name at all, but the colleagues' own analysis output quotes
# `useCase=[M2101] FPS Inward credit Success`, and that id joins straight into our catalog.
# Candidates are VERIFIED against the snapshot before being reported — same discipline as repo ids,
# a pattern match alone is a guess.
_USE_CASE_ID = re.compile(r"\b([A-Z]\d{4})\b")

_MAX_TOPICS = 25
_MAX_USE_CASES_LISTED = 40


def _load_patterns():
    """The intranet-owned knob file; every lookup below tolerates it being absent or partial."""
    try:
        with open(config.ALARM_PATTERNS_JSON, encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tokens(patterns, section, key, default):
    block = patterns.get(section)
    if isinstance(block, dict):
        values = block.get(key)
        if isinstance(values, list):
            cleaned = [str(v).strip() for v in values if str(v).strip()]
            if cleaned:
                return tuple(cleaned)
    return tuple(default)


def _tz_aliases(patterns):
    block = patterns.get("timezones")
    if isinstance(block, dict) and isinstance(block.get("aliases"), dict):
        aliases = {str(k).strip().upper(): str(v).strip()
                   for k, v in block["aliases"].items() if str(k).strip() and str(v).strip()}
        if aliases:
            return aliases
    return dict(_DEFAULT_TZ_ALIASES)


def _known_repos(repos=None):
    """The repo universe to scan for. Injectable because index/repo_tags.json is gitignored — the
    external side has no copy, so tests (and any caller holding its own list) pass one in."""
    if repos is not None:
        return sorted({str(r).strip() for r in repos if str(r).strip()})
    try:
        return sorted(repo_tags.load(missing_ok=False).keys())
    except (FileNotFoundError, ValueError):
        return []


def _boundary_ok(text, start, end):
    before_ok = start == 0 or not _ALNUM.match(text[start - 1])
    after_ok = end >= len(text) or not _ALNUM.match(text[end])
    return before_ok and after_ok


def _find_repo_spans(text, known):
    spans = []
    for repo in known:
        offset = 0
        while True:
            index = text.find(repo, offset)
            if index < 0:
                break
            if _boundary_ok(text, index, index + len(repo)):
                spans.append((index, index + len(repo), repo))
            offset = index + 1
    return spans


def _drop_subsumed(spans):
    """Keep only maximal matches: 'a-b' inside 'a-b-job' is the same occurrence, not a second repo."""
    kept = []
    for start, end, repo in spans:
        covered = any(
            (o_start <= start and o_end >= end) and (o_start, o_end) != (start, end)
            for o_start, o_end, _ in spans
        )
        if not covered:
            kept.append((start, end, repo))
    return sorted(set(kept))


def _hint_repos(text, patterns, known):
    """Fallback for the ~7% of alarms naming a shared resource (an RDS cluster, a queue) instead of
    a repo. These are hand-asserted by the intranet side, so they are reported as `candidate` and
    never as `confirmed` — the assistant must say it is an assertion, not evidence."""
    hints = patterns.get("resource_repo_hints")
    if not isinstance(hints, dict):
        return []
    lowered = text.lower()
    known_set = set(known)
    out = []
    for fragment, repos in hints.items():
        if str(fragment).startswith("_") or not isinstance(repos, list):
            continue
        if str(fragment).strip() and str(fragment).strip().lower() in lowered:
            for repo in repos:
                repo = str(repo).strip()
                if repo and repo in known_set:
                    out.append({"repo": repo, "matched_text": str(fragment).strip(),
                                "confidence": "candidate",
                                "why": f"config/alarm_patterns.json resource_repo_hints "
                                       f"['{fragment}'] — hand-asserted mapping, not evidence"})
    return out


def _known_use_cases(candidates):
    """Keep only ids that actually exist in the routing snapshot, with their topics.

    An id-shaped string is not evidence of a use case — `A1234` occurs in plenty of text. Verifying
    against the snapshot is what turns a pattern match into a fact, and it means an unrecognised id
    can be reported as exactly that rather than silently becoming a business impact claim."""
    found, unknown = [], []
    for candidate in candidates:
        route = messages.usecase_route(use_case_id=candidate)
        matches = [m for m in (route.get("matches") or [])
                   if (m.get("use_case") or "").strip().upper() == candidate]
        if matches:
            found.append({
                "use_case": candidate,
                "topics": sorted({(m.get("topic") or "").strip() for m in matches if m.get("topic")}),
            })
        else:
            unknown.append(candidate)
    return found, unknown


def _extract_times(text, aliases):
    """Times matter only with a timezone attached. RUNBOOK-55 confirmed THREE coexist (CloudWatch
    UTC / LogDream Asia/Hong_Kong / server GMT), so a bare clock time is reported as ambiguous
    rather than silently assumed — assuming it is the single easiest way to look 30 minutes into
    the wrong window, find nothing, and report 'no anomaly'."""
    out, taken = [], []

    def _add(match):
        stamp = match.group(1)
        suffix = (match.group(2) or "").strip().upper()
        zone = aliases.get(suffix) if suffix else None
        if not zone and stamp.endswith("Z"):
            zone = "UTC"
        out.append({
            "text": stamp + ((" " + suffix) if suffix else ""),
            "timezone": zone or "",
            "ambiguous": not zone,
        })

    for match in _DATETIME.finditer(text):
        taken.append((match.start(1), match.end(1)))
        _add(match)
    for match in _CLOCK.finditer(text):
        # The clock pattern also matches the time half of a full datetime already reported above;
        # skip anything sitting inside one so a single stamp is not counted twice.
        if any(start <= match.start(1) and match.end(1) <= end for start, end in taken):
            continue
        _add(match)
    return out


def parse_alert(text, repos=None):
    """Structure a raw alert string. Repo identification is a known-id scan (see module docstring);
    every other field is best-effort commentary that degrades to empty."""
    text = (text or "").strip()
    patterns = _load_patterns()
    known = _known_repos(repos)

    result = {
        "identified": False,
        "repos": [],
        "environment": "",
        "resource_type": "",
        "severity": "",
        "metric": "",
        "threshold": "",
        "delivery_path": {"phrase": "", "resolved_id": None, "note": ""},
        "use_cases": [],
        "times": [],
        "notes": [],
    }
    if not text:
        result["notes"].append("empty alert text")
        return result

    # Use-case ids are found first and independently of the repo scan: the largest alert family
    # ("MDC Alert - General SHP API Error") never names a repo, so this is its only way in.
    verified, unknown_ids = _known_use_cases(sorted(set(_USE_CASE_ID.findall(text))))
    result["use_cases"] = verified
    if unknown_ids:
        result["notes"].append(
            f"these look like use-case ids but are not in the routing snapshot: "
            f"{', '.join(unknown_ids)} — treat as unrecognised, not as affected business.")

    if not known:
        result["notes"].append(
            "repo universe unavailable (index/repo_tags.json missing and no repos= supplied) — "
            "cannot identify the affected repo from the text. Run make_repo_tags.py on the box.")
        result["identified"] = bool(verified)
        return result

    matched = _drop_subsumed(_find_repo_spans(text, known))
    result["repos"] = [
        {"repo": repo, "matched_text": text[start:end], "confidence": "confirmed",
         "why": f"exact repo id present in the alert text at offset {start}"}
        for start, end, repo in matched
    ]
    if not result["repos"]:
        hinted = _hint_repos(text, patterns, known)
        result["repos"] = hinted
        if not hinted and not verified:
            result["notes"].append(
                "no known repo id appears in this alert text, no resource_repo_hints entry "
                "matched, and no recognised use-case id was present. NOT guessing. To fix "
                "permanently, add the resource->repo mapping to config/alarm_patterns.json "
                "(intranet-owned).")
    # Either route is enough to say something true: a repo, or a verified use case.
    result["identified"] = bool(result["repos"]) or bool(verified)

    lowered = text.lower()
    for env in _tokens(patterns, "environments", "prefixes", _DEFAULT_ENVIRONMENTS):
        if lowered.startswith(env.lower()) or (env.lower() + "ecs") in lowered:
            result["environment"] = env
            break
    for resource in _tokens(patterns, "resource_types", "tokens", _DEFAULT_RESOURCE_TYPES):
        if resource.lower() in lowered:
            result["resource_type"] = resource
            break
    for severity in _tokens(patterns, "severities", "tokens", _DEFAULT_SEVERITIES):
        if severity in text:
            result["severity"] = severity
            break
    for metric in _tokens(patterns, "metrics", "tokens", ()):
        if metric.lower() in lowered:
            result["metric"] = metric
            break
    threshold = _THRESHOLD.search(text)
    if threshold:
        result["threshold"] = threshold.group(1)

    phrase = _PATH_PHRASE.search(text)
    if phrase:
        name_map = ((patterns.get("delivery_path") or {}).get("name_to_id")
                    if isinstance(patterns.get("delivery_path"), dict) else None)
        resolved = (name_map or {}).get(phrase.group(1))
        result["delivery_path"] = {
            "phrase": phrase.group(1),
            "resolved_id": resolved,
            "note": ("" if resolved else
                     "delivery_path name->id map not supplied yet (RUNBOOK-56 Q3): "
                     "tbl_use_case_router.delivery_path is a numeric enum, so this phrase cannot "
                     "be joined to the router table. Reported verbatim, NOT resolved."),
        }
    result["times"] = _extract_times(text, _tz_aliases(patterns))
    if any(t["ambiguous"] for t in result["times"]):
        result["notes"].append(
            "at least one time in this alert carries no timezone. Three coexist here "
            "(CloudWatch UTC / LogDream Asia/Hong_Kong / server GMT) — do not assume one.")
    return result


def _channel_bound(channels, per_channel_sample=5):
    """Channel-level UPPER BOUND on business impact, straight from the UAT use-case catalog.

    The precise answer is repo -> topic -> use case, but that join needs a same-environment route
    table and RUNBOOK-57 showed the only one present is a stale dev/SCT export whose topics barely
    intersect the code-derived message edges. Falling back to "every use case configured on this
    repo's channel" is coarse — it over-counts, because not every SMS use case flows through every
    SMS repo — but it is TRUE and it is an upper bound, which in an incident is the safe direction
    to be wrong in. Reporting nothing at all would read as "no business impact", which is the
    dangerous direction."""
    out = {"method": "channel_upper_bound", "by_channel": [], "total_upper_bound": 0,
           "caveat": ("UPPER BOUND, not the affected set: these are all use cases configured on "
                      "this repo's channel(s), including ones that do not flow through this "
                      "particular service. Say 'at most N' and never read a number here as the "
                      "confirmed blast radius.")}
    seen_total = 0
    for channel in channels or []:
        try:
            found = usecase_catalog.search_usecases(channel=channel, limit=per_channel_sample)
        except Exception:  # noqa: BLE001 — a missing catalog must not break incident triage
            continue
        if not found.get("available"):
            continue
        total = int(found.get("total") or 0)
        seen_total += total
        out["by_channel"].append({
            "channel": channel,
            "use_case_count": total,
            "sample": [item.get("use_case_id") for item in (found.get("items") or [])
                       if item.get("use_case_id")],
        })
    out["total_upper_bound"] = seen_total
    out["available"] = bool(out["by_channel"])
    return out


def blast_radius(repo, max_topics=_MAX_TOPICS, max_use_cases=_MAX_USE_CASES_LISTED):
    """Repo -> message topics it is wired to -> the business use cases routed onto those topics.

    Both hops read existing snapshots (``messages.routes_for_repo`` over index/message_edges.csv,
    ``messages.reverse_lookup_use_cases`` over the dev/SCT use-case export), so every use case
    carries the snapshot citation and the dev/SCT-vs-production caveat those functions already
    attach. Nothing here is inferred."""
    repo = (repo or "").strip()
    if not repo:
        return {"repo": "", "available": False, "error": "repo is required"}

    edges = messages.routes_for_repo(repo)
    directions = {}
    for edge in edges:
        destination = (edge.get("destination") or "").strip()
        if not destination:
            continue
        role = "produce" if edge.get("producer_repo") == repo else "consume"
        directions.setdefault(destination, set()).add(role)

    topics = sorted(directions)
    listed_topics = topics[:max_topics]

    # The topic -> use-case join is only meaningful when the active dataset ships its OWN route
    # table. usecase_catalog guards this (its "defect #2": UAT coverage computed off the stale
    # dev/SCT route file) — calling messages.reverse_lookup_use_cases directly bypassed that guard,
    # which is how RUNBOOK-57 ended up with 0 use cases on every real alert and no explanation.
    # Zero must never be reported as "no business impact" when the truth is "wrong table".
    route = usecase_catalog.route_dimension()
    use_cases, seen = [], set()
    snapshot_source = None
    if route.get("available"):
        for topic in listed_topics:
            found = messages.reverse_lookup_use_cases(topic, exact=True, limit=0)
            snapshot_source = snapshot_source or found.get("source")
            if not found.get("available"):
                continue
            for item in found.get("items") or []:
                key = (item.get("use_case"), item.get("topic"))
                if key in seen:
                    continue
                seen.add(key)
                use_cases.append(item)

    tags = repo_tags.for_repo(repo)
    channels = sorted({c for c in (list(tags.get("channel") or [])
                                   + list(tags.get("msg_channels") or [])
                                   + list(tags.get("serves_channels") or [])) if c})

    # Why the precise use-case answer is or is not available — never a bare zero.
    if not route.get("available"):
        use_case_link = {
            "method": "topic", "available": False,
            "reason": (f"no same-environment use-case route table in the active dataset "
                       f"({route.get('reason') or 'unavailable'}). The precise repo -> topic -> "
                       f"use-case join is therefore NOT computable right now."),
            "do_not_conclude": ("This is 'cannot compute', NOT 'no use cases affected'. Do not "
                                "tell anyone this incident has no business impact on the strength "
                                "of this field."),
        }
    elif not use_cases:
        use_case_link = {
            "method": "topic", "available": True, "matched": 0,
            "reason": ("the route table is present but none of this repo's topics appear in it. "
                       "RUNBOOK-57 measured only 3 topics in common between the code-derived "
                       "message edges (255 topics) and the route snapshot (20 topics), so a zero "
                       "here usually means the two snapshots do not cover the same ground."),
            "do_not_conclude": "Zero matches is NOT evidence of zero business impact.",
        }
    else:
        use_case_link = {"method": "topic", "available": True, "matched": len(use_cases)}

    return {
        "repo": repo,
        "available": True,
        "topics": [{"topic": topic, "direction": sorted(directions[topic])} for topic in listed_topics],
        "topic_count": len(topics),
        "topics_truncated": len(topics) > len(listed_topics),
        "use_case_link": use_case_link,
        "use_case_total": len(use_cases),
        "use_cases": use_cases[:max_use_cases],
        "use_cases_truncated": len(use_cases) > max_use_cases,
        # Coarse but real, and available today — see _channel_bound.
        "channel_upper_bound": _channel_bound(channels),
        "channels": channels,
        "business_line": tags.get("business_line") or "",
        "time_critical": bool(tags.get("time_critical")),
        "snapshot": snapshot_source,
        "vendor": None,
        # Read at call time: this used to hardcode "not ingested", which became a false statement
        # the moment the intranet ingested the table (RUNBOOK-54).
        "vendor_note": (usecase_catalog.router_table_status()["note"]
                        + " Do NOT infer a vendor from repo names in an incident answer."),
        "caveats": [
            "use cases come from the routing snapshot — indicative, verify against production "
            "before telling anyone their traffic stopped.",
            "topics come from the message-edge snapshot; a repo with no edges yields no use cases, "
            "which means 'not visible in this snapshot', NOT 'affects nobody'.",
            "read `use_case_link` before quoting any use-case number: it says whether the precise "
            "join was computable at all. A zero there is never proof of no impact.",
        ],
    }


def blast_radius_for_use_case(use_case, topics):
    """The other direction: a verified use case -> its topics -> the repos on them.

    Used for the alert family that names no repo. Reports the repos as ``candidate``: being on the
    same topic makes a repo *involved in the path*, which is weaker evidence than an alert naming
    the service outright, and an incident answer must not present the two as equally certain."""
    involved = {}
    for topic in topics or []:
        for edge in messages.who_produces(topic):
            repo = (edge.get("producer_repo") or "").strip()
            if repo:
                involved.setdefault(repo, {"repo": repo, "topics": set(), "roles": set()})
                involved[repo]["topics"].add(topic)
                involved[repo]["roles"].add("produce")
        for edge in messages.who_consumes(topic):
            repo = (edge.get("consumer_repo") or "").strip()
            if repo:
                involved.setdefault(repo, {"repo": repo, "topics": set(), "roles": set()})
                involved[repo]["topics"].add(topic)
                involved[repo]["roles"].add("consume")

    tags = {repo: repo_tags.for_repo(repo) for repo in involved}
    channels = sorted({c for meta in tags.values()
                       for c in (list(meta.get("channel") or []) + list(meta.get("msg_channels") or []))
                       if c})
    return {
        "use_case": use_case,
        "available": True,
        "topics": list(topics or []),
        "repos": [{"repo": entry["repo"], "topics": sorted(entry["topics"]),
                    "roles": sorted(entry["roles"]), "confidence": "candidate",
                    "why": "on a topic this use case routes to (path involvement, not a named "
                           "failure)"}
                   for entry in sorted(involved.values(), key=lambda e: e["repo"])],
        "channels": channels,
        "vendor": None,
        "vendor_note": usecase_catalog.router_table_status()["note"],
        "caveats": [
            "use-case -> topic comes from the dev/SCT routing snapshot — indicative, verify "
            "against production.",
            "repos here share a topic with the use case; that is weaker than an alert naming the "
            "service. Say 'involved in this path', not 'this repo failed'.",
        ],
    }


def incident_impact(alert_text, repos=None, max_use_cases=_MAX_USE_CASES_LISTED):
    """The tool entry point: paste an alert, get who is affected and who to tell.

    Reads no production data. When the repo cannot be identified the answer says so and stops —
    an incident answer that guesses the wrong service is worse than no answer."""
    parsed = parse_alert(alert_text, repos=repos)
    result = {
        "ok": True,
        "source": "local artefacts only — no MCP, no production logs, no AWS calls",
        "parsed": parsed,
        "affected": [],
    }
    if not parsed["identified"]:
        result["ok"] = False
        result["error"] = ("could not identify an affected repo OR a known use case from this "
                           "alert text")
        result["next_step"] = (
            "Tell the user which part of the alert you could and could not read, and ask for the "
            "service/repo name or the use-case id. Do not guess.")
        return result

    for entry in parsed["repos"]:
        radius = blast_radius(entry["repo"], max_use_cases=max_use_cases)
        radius["identified_via"] = entry["confidence"]
        radius["identified_why"] = entry["why"]
        result["affected"].append(radius)

    # The use-case route, for the alert family that names no repo at all.
    result["affected_use_cases"] = [
        blast_radius_for_use_case(item["use_case"], item["topics"])
        for item in parsed["use_cases"]
    ]

    result["totals"] = {
        "repos": len(result["affected"]),
        "use_cases_from_repos": sum(item.get("use_case_total", 0) for item in result["affected"]),
        "use_cases_named_in_alert": len(result["affected_use_cases"]),
        "channels": sorted(
            {c for item in result["affected"] for c in item.get("channels") or []}
            | {c for item in result["affected_use_cases"] for c in item.get("channels") or []}),
    }
    return result
