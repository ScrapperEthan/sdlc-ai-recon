"""Async message-wiring queries over index/message_edges.csv plus the
use-case -> topic snapshot (dev/SCT). All read-only."""
import csv
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


def _detect(cols, *needles):
    for c in cols:
        flat = c.lower().replace('_', '')
        if all(x in flat for x in needles):
            return c
    return None


def usecase_route(use_case_id=None, topic=None):
    rows, cols = _load_usecase()
    if not rows:
        return {
            "available": False,
            "note": ("use-case routing snapshot not present "
                     "(index/tbl_event_router_usecase_topic.snapshot.csv). "
                     "The async last-mile is DB-driven; export the table read-only to enable this."),
        }
    uc = _detect(cols, 'usecase', 'id') or _detect(cols, 'usecase')
    tp = _detect(cols, 'topic')
    matches = []
    for r in rows:
        if use_case_id and use_case_id.lower() not in (r.get(uc, '') or '').lower():
            continue
        if topic and topic.lower() not in (r.get(tp, '') or '').lower():
            continue
        matches.append({"use_case": r.get(uc, ''), "topic": r.get(tp, ''), "row": r})
    return {
        "available": True,
        "source": "dev/SCT snapshot — indicative, verify vs prod",
        "usecase_col": uc,
        "topic_col": tp,
        "matches": matches,
    }
