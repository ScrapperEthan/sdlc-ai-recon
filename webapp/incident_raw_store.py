"""Retained raw log text, so a devops tester can check an evidence item against the original.

Evidence you cannot verify is evidence people will not trust. During the UAT internal test the
devops team needs to click a redacted excerpt and read the real lines, otherwise they have no way to
tell a correct investigator from a plausible one. That is a good reason, and this module exists to
serve it **without** giving up the property the whole design rests on.

## The one thing this does not do

The model never sees any of this. `incident_investigator` puts a `ref` on each evidence item and the
raw lines go in here; the browser fetches them on demand. Raw text therefore:

* never enters the model's context, and
* never RE-enters it on later turns — which is the specific reason raw logs are not written into
  `chat_sessions.json` itself. `session_store.history_for_agent` replays the transcript to the model
  every turn, so a log stored there would be re-read on every follow-up question for the life of the
  conversation. The retention the owner asked for is delivered; the context contamination that would
  have come free with it is not.

## Bounded on purpose

Off by default. When on: capped entry count (oldest dropped), a TTL enforced on both read and write,
and a per-entry line cap. Unbounded retention of production log text is how a testing convenience
becomes a data-retention finding six months later. Purging is deleting one file.

## Owner-scoped

Reads require the same opaque per-browser owner id that `session_store` uses, and a wrong owner is
indistinguishable from a missing ref. Without that, one tester's browser could pull another's raw
production logs out of a shared deployment.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import config

_LOCK = threading.Lock()


def enabled():
    return bool(config.INCIDENT_RAW_LOGS)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


def _parse(stamp):
    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_unlocked():
    if not os.path.exists(config.INCIDENT_RAW_STORE):
        return {"entries": []}
    try:
        with open(config.INCIDENT_RAW_STORE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        # A corrupt retention file must not break an investigation. Losing retained text costs a
        # click-through; refusing to investigate costs the incident.
        return {"entries": []}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return {"entries": []}
    return data


def _save_unlocked(data):
    parent = os.path.dirname(config.INCIDENT_RAW_STORE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp_path = config.INCIDENT_RAW_STORE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, config.INCIDENT_RAW_STORE)


def _expired(entry, now=None):
    now = now or _now()
    stored = _parse(entry.get("stored_at"))
    if stored is None:
        return True                                   # undatable == unretainable
    return stored < now - timedelta(hours=max(0, config.INCIDENT_RAW_TTL_HOURS))


def _sweep(entries):
    """Drop expired entries, then the oldest beyond the cap."""
    now = _now()
    kept = [entry for entry in entries if not _expired(entry, now)]
    cap = max(1, config.INCIDENT_RAW_MAX_ENTRIES)
    return kept[-cap:] if len(kept) > cap else kept


def put(owner, lines, meta=None):
    """Retain `lines` and return an opaque ref, or "" when retention is off.

    Returning "" rather than raising is deliberate: an investigation must run identically whether or
    not retention is enabled, so the only difference is whether the evidence carries a ref.
    """
    if not enabled():
        return ""
    lines = [str(line) for line in (lines or [])]
    capped = max(1, config.INCIDENT_RAW_MAX_LINES)
    truncated = len(lines) > capped
    entry = {
        "ref": uuid.uuid4().hex,
        # Same opaque per-browser id session_store uses. No owner => not retrievable by anyone.
        "owner": owner or "",
        "stored_at": _stamp(_now()),
        "lines": lines[:capped],
        "line_count": len(lines),
        "truncated": truncated,
        "meta": dict(meta or {}),
    }
    with _LOCK:
        data = _load_unlocked()
        data["entries"] = _sweep(list(data["entries"]) + [entry])
        _save_unlocked(data)
    return entry["ref"]


def get(ref, owner):
    """The retained entry, or None. A wrong owner is indistinguishable from a missing ref."""
    ref = (ref or "").strip()
    if not ref or not enabled():
        return None
    with _LOCK:
        data = _load_unlocked()
        for entry in data["entries"]:
            if entry.get("ref") != ref:
                continue
            if not owner or entry.get("owner") != owner:
                return None
            if _expired(entry):
                return None
            return {
                "ref": entry["ref"],
                "stored_at": entry.get("stored_at") or "",
                "lines": list(entry.get("lines") or []),
                "line_count": entry.get("line_count", 0),
                "truncated": bool(entry.get("truncated")),
                "meta": dict(entry.get("meta") or {}),
                "warning": ("RAW production log text, unredacted. Retained only because "
                            "SDLC_INCIDENT_RAW_LOGS is on for the UAT internal test. Do not paste "
                            "this into a ticket, a chat, or anywhere it will be stored again."),
            }
    return None


def purge(owner=None):
    """Delete retained text — everything, or just one owner's. Returns how many entries went."""
    with _LOCK:
        data = _load_unlocked()
        before = len(data["entries"])
        if owner:
            data["entries"] = [e for e in data["entries"] if e.get("owner") != owner]
        else:
            data["entries"] = []
        _save_unlocked(data)
        return before - len(data["entries"])


def status():
    """What is retained right now — for the status endpoint and for anyone auditing the test."""
    if not enabled():
        return {"enabled": False,
                "note": "raw log retention is off; evidence carries no click-through ref"}
    with _LOCK:
        data = _load_unlocked()
        live = [entry for entry in data["entries"] if not _expired(entry)]
        return {
            "enabled": True,
            "entries": len(live),
            "expired_pending_sweep": len(data["entries"]) - len(live),
            "max_entries": config.INCIDENT_RAW_MAX_ENTRIES,
            "ttl_hours": config.INCIDENT_RAW_TTL_HOURS,
            "max_lines_per_entry": config.INCIDENT_RAW_MAX_LINES,
            "store": config.INCIDENT_RAW_STORE,
            "oldest": min((e.get("stored_at") or "" for e in live), default=""),
            "warning": ("UNREDACTED production log text is being retained on disk. This is a UAT "
                         "internal-test setting (owner decision 2026-07-30) and must be turned off "
                         "before production use. The model never receives it; only the browser can "
                         "fetch it, scoped to the session owner."),
        }
