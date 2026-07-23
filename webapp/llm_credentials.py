"""RAM-only credential store for the internal-beta paste-token "direct Copilot" mode.

THROWAWAY feature (see docs/specs/copilot-token-direct-mode.md), behind `SDLC_LLM_TOKEN_MODE`
(default off). Mirrors `webapp/llm_routes.py`'s shape (opaque id -> record, lock-guarded,
connect/resolve/describe helpers) but deliberately does NOT mirror its disk persistence: this store
is a plain in-memory dict, nothing is ever written to `llm_routes.json` or any other file, and
nothing here is ever logged. A pasted `.copilot_token` is one of the few genuinely sensitive things
this app ever touches, so keeping it out of any file/log is the whole point of this module.

    credential_id -> {
        "owner_uid": str,                    # the browser uid that connected it (session hygiene)
        "oauth_token": str,                  # the pasted .copilot_token -- RAM only, never persisted
        "service_token": str | None,         # cached short-lived Copilot service token (stage 2)
        "service_token_expiry": float | None,  # epoch seconds, set by github_copilot_direct.py
        "created_at": str,
    }

Process-memory only: restarting the server (or the process dying) drops every credential, which is
the correct behaviour for a throwaway internal beta (no vault, no migration to undo -- see spec §9).
"""
import threading
import uuid
from datetime import datetime, timezone

_LOCK = threading.Lock()
_STORE = {}  # credential_id -> record. Intentionally a plain dict: RAM only, never touches disk.


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(oauth_token, owner_uid=""):
    """Store a freshly pasted `.copilot_token` in RAM; returns a new opaque `credential_id`.

    Raises ValueError if the token is blank -- never logs the token itself, in the exception message
    or anywhere else."""
    oauth_token = (oauth_token or "").strip()
    if not oauth_token:
        raise ValueError("token is required")
    credential_id = uuid.uuid4().hex
    with _LOCK:
        _STORE[credential_id] = {
            "owner_uid": (owner_uid or "").strip(),
            "oauth_token": oauth_token,
            "service_token": None,
            "service_token_expiry": None,
            "created_at": _now(),
        }
    return credential_id


def disconnect(credential_id):
    """Drop a credential from RAM. Idempotent: returns True if something was actually removed,
    False if the id was already gone/unknown -- either way it's gone."""
    credential_id = (credential_id or "").strip()
    if not credential_id:
        return False
    with _LOCK:
        return _STORE.pop(credential_id, None) is not None


def resolve(credential_id):
    """A COPY of the RAM record for a credential_id, or None if unknown/disconnected (fails closed --
    callers must never fall back to a shared/default endpoint just because this returns None).

    Returns a copy (not the live dict) so a caller mutating its own local variable can't accidentally
    corrupt the store outside the lock; use `update_service_token` to write back."""
    credential_id = (credential_id or "").strip()
    if not credential_id:
        return None
    with _LOCK:
        record = _STORE.get(credential_id)
        return dict(record) if record is not None else None


def update_service_token(credential_id, service_token, expiry):
    """Cache the derived short-lived Copilot service token (stage 2 of the token exchange) against
    this credential. No-op (returns False) if the credential was disconnected concurrently."""
    credential_id = (credential_id or "").strip()
    with _LOCK:
        record = _STORE.get(credential_id)
        if record is None:
            return False
        record["service_token"] = service_token
        record["service_token_expiry"] = expiry
        return True


def describe(credential_id):
    """Public view for the UI's 'my LLM' panel -- never includes oauth_token/service_token, only
    whether a credential is connected."""
    record = resolve(credential_id)
    if not record:
        return {"connected": False}
    return {"connected": True, "mode": "copilot_token", "created_at": record.get("created_at")}


def count():
    """Number of credentials currently held in RAM. Test/introspection helper -- there is no disk
    file to inspect instead, this IS the store."""
    with _LOCK:
        return len(_STORE)
