"""Async message-wiring queries over index/message_edges.csv plus the
use-case -> topic snapshot (dev/SCT). All read-only."""
import csv
import os
import re
from datetime import datetime, timezone
from . import config

_EDGE_COLS = ('producer_repo', 'destination', 'consumer_repo', 'routing_source', 'evidence')


def _load_edges():
    try:
        with open(config.MESSAGE_EDGES_CSV, newline='', encoding='utf-8-sig') as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _edge(r):
    return {k: (r.get(k) or '').strip() for k in _EDGE_COLS}


def who_consumes(destination):
    d = destination.lower()
    return [_edge(r) for r in _load_edges()
            if d in (r.get('destination') or '').lower() and (r.get('consumer_repo') or '').strip()]


def who_produces(destination):
    d = destination.lower()
    return [_edge(r) for r in _load_edges()
            if d in (r.get('destination') or '').lower() and (r.get('producer_repo') or '').strip()]


def routes_for_repo(repo):
    out = []
    for r in _load_edges():
        if repo in ((r.get('producer_repo') or '').strip(), (r.get('consumer_repo') or '').strip()):
            out.append(_edge(r))
    return out


# --- use-case -> topic snapshot (the async "last mile"; dev/SCT, verify vs prod) ---

def _load_usecase():
    try:
        with open(config.USECASE_SNAPSHOT_CSV, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        cols = list(rows[0].keys()) if rows else []
        return rows, cols
    except FileNotFoundError:
        return [], []


def _usecase_columns(cols):
    return _detect(cols, 'usecase', 'id') or _detect(cols, 'usecase'), _detect(cols, 'topic')


def _snapshot_citation(line_no):
    try:
        path = config.USECASE_SNAPSHOT_CSV
        root = config.ROOT
        path = os.path.relpath(path, root)
    except ValueError:
        path = config.USECASE_SNAPSHOT_CSV
    return path.replace(os.sep, '/') + ':' + str(line_no)


def _load_usecase_with_lines():
    try:
        with open(config.USECASE_SNAPSHOT_CSV, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            return [(line_no, row) for line_no, row in enumerate(reader, 2)], cols
    except FileNotFoundError:
        return [], []


def _detect(cols, *needles):
    for c in cols:
        flat = c.lower().replace('_', '')
        if all(x in flat for x in needles):
            return c
    return None


def _snapshot_manifest():
    """Provenance for the use-case->topic snapshot so an answer never presents it as production
    truth. 'Not found here' means 'not in this dev/SCT export', NOT 'does not exist in prod'."""
    path = config.USECASE_SNAPSHOT_CSV
    manifest = {
        "environment": "dev/SCT",
        "source_table": "tbl_event_router_usecase_topic",
        "exported_at": None,
        "row_count": 0,
        "production_verified": False,
        "caveat": ("dev/SCT snapshot — indicative, NOT production. Absence here does not prove "
                   "absence in production."),
    }
    try:
        mtime = os.path.getmtime(path)
        manifest["exported_at"] = (
            datetime.fromtimestamp(mtime, timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
    except OSError:
        pass
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            manifest["row_count"] = max(0, sum(1 for _ in handle) - 1)  # minus header
    except OSError:
        pass
    return manifest


def usecase_route(use_case_id=None, topic=None):
    rows, cols = _load_usecase()
    if not rows:
        return {
            "available": False,
            "note": ("use-case routing snapshot not present "
                     "(index/tbl_event_router_usecase_topic.snapshot.csv). "
                     "The async last-mile is DB-driven; export the table read-only to enable this."),
        }
    uc, tp = _usecase_columns(cols)
    matches = []
    for r in rows:
        if use_case_id and use_case_id.lower() not in (r.get(uc, '') or '').lower():
            continue
        if topic and topic.lower() not in (r.get(tp, '') or '').lower():
            continue
        matches.append({"use_case": r.get(uc, ''), "topic": r.get(tp, ''), "row": r})
    # Make the query's SEMANTICS explicit — the old tool silently AND-filtered both params, so a
    # "topic -> which use cases" question that also carried a known use_case_id got narrowed to that
    # one use case and hid the siblings. Name the mode and, for the pair case, point at the reverse
    # tool that actually lists a topic's other use cases.
    mode = ("pair_verification" if (use_case_id and topic)
            else "use_case_to_topics" if use_case_id
            else "topic_to_use_cases" if topic else "list_all")
    result = {
        "available": True,
        "mode": mode,
        "source": "dev/SCT snapshot — indicative, verify vs prod",
        "usecase_col": uc,
        "topic_col": tp,
        "matches": matches,
    }
    if use_case_id and topic:
        result["note"] = ("Filtered to this ONE use_case + topic pair (verification). It does NOT "
                          "list other use cases on the topic.")
        result["hint"] = "To list every use case sharing this topic, call use_cases_for_topic(topic)."
    elif topic and not use_case_id:
        result["match"] = "substring"
        result["hint"] = ("Substring match — may span multiple distinct topics. For a known full "
                          "topic, call use_cases_for_topic(topic, exact=true).")
    return result


def reverse_lookup_use_cases(topic, exact=True, limit=50):
    """Given a TOPIC, list every use case that routes to it — the reverse of usecase->topic.

    ``exact`` (default) compares the FULL topic string case-insensitively — the honest default for a
    known topic. ``exact=False`` is a substring probe that may span several distinct topics, which
    are reported separately (``matched_topics``) so different systems/prefixes aren't silently
    merged. Always returns a valid, paginated envelope (``total``/``returned``/``truncated`` — never
    a byte-truncated blob) plus snapshot provenance, so the caller can separate 'not in this dev/SCT
    snapshot' from 'not in production'."""
    match = "exact" if exact else "substring"
    manifest = _snapshot_manifest()
    topic = (topic or "").strip()
    if not topic:
        return {"query": {"topic": "", "match": match}, "source": manifest, "available": False,
                "total": 0, "returned": 0, "truncated": False, "items": [], "error": "topic is required"}

    rows, cols = _load_usecase_with_lines()
    uc, tp = _usecase_columns(cols)
    if not uc or not tp:
        return {"query": {"topic": topic, "match": match}, "source": manifest, "available": False,
                "total": 0, "returned": 0, "truncated": False, "items": [],
                "note": "use-case routing snapshot not present or missing use_case/topic columns."}

    needle = topic.lower()
    matches, matched_topics, seen = [], set(), set()
    for line_no, row in rows:
        row_topic = (row.get(tp) or "").strip()
        use_case = (row.get(uc) or "").strip()
        if not use_case:
            continue
        hit = (row_topic.lower() == needle) if exact else (needle in row_topic.lower())
        if not hit:
            continue
        key = (use_case, row_topic)
        if key in seen:  # dedupe repeated snapshot rows so shared topics aren't inflated
            continue
        seen.add(key)
        matched_topics.add(row_topic)
        matches.append({"use_case": use_case, "topic": row_topic,
                        "citation": _snapshot_citation(line_no)})

    total = len(matches)
    limited = matches[:limit] if limit and limit > 0 else matches
    result = {
        "query": {"topic": topic, "match": match},
        "source": manifest,
        "available": True,
        "total": total,
        "returned": len(limited),
        "truncated": total > len(limited),
        "items": limited,
    }
    if not exact:
        result["matched_topics"] = sorted(matched_topics)
        result["distinct_topic_count"] = len(matched_topics)
    return result


def use_cases_for_topic(topic):
    """Reverse lookup with snapshot-row citations; missing snapshot is harmless."""
    rows, cols = _load_usecase_with_lines()
    uc, tp = _usecase_columns(cols)
    if not uc or not tp:
        return []
    needle = (topic or '').strip().lower()
    if not needle:
        return []
    return [
        {
            "use_case": (row.get(uc) or '').strip(),
            "topic": (row.get(tp) or '').strip(),
            "citations": [_snapshot_citation(line_no)],
        }
        for line_no, row in rows
        if needle == (row.get(tp) or '').strip().lower() and (row.get(uc) or '').strip()
    ]


def use_cases_for_channel(channel):
    """Reverse lookup for topics containing an exact channel token."""
    token = (channel or '').strip().lower()
    if not token:
        return []
    rows, cols = _load_usecase_with_lines()
    uc, tp = _usecase_columns(cols)
    if not uc or not tp:
        return []
    out = []
    for line_no, row in rows:
        topic = (row.get(tp) or '').strip()
        tokens = {part.lower() for part in re.split(r"[^a-zA-Z0-9]+", topic) if part}
        if token in tokens and (row.get(uc) or '').strip():
            out.append({
                "use_case": (row.get(uc) or '').strip(),
                "topic": topic,
                "citations": [_snapshot_citation(line_no)],
            })
    return out
