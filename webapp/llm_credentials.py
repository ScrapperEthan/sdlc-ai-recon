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
        "selected_model": str | None,        # the model confirmed by a real llm.chat() probe
        "created_at": str,
    }

Process-memory only: restarting the server (or the process dying) drops every credential, which is
the correct behaviour for a throwaway internal beta (no vault, no migration to undo -- see spec §9).
"""
import re
import threading
import uuid
from datetime import datetime, timezone

_LOCK = threading.Lock()
_STORE = {}  # credential_id -> record. Intentionally a plain dict: RAM only, never touches disk.

# Every credential_id carries this prefix so a caller (or the provider re-verifying ownership) can
# recognize "this looks like one of ours" without a store lookup -- see is_credential_id() below,
# used by server.py to keep routing a stale/disconnected id at the direct provider (which then fails
# closed) instead of silently falling back to the shared default LLM.
_CREDENTIAL_ID_PREFIX = "ct_"

# Model ids look like "gpt-5.5", "claude-3-5-sonnet-20241022", or namespaced "openai/gpt-4o" --
# never free text. Rejecting anything else keeps a bad/garbled model id a clean 400 instead of it
# silently riding into a provider request payload.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_model_id(model_id):
    """Return a stripped, validated model id, or raise ValueError. Never logs/echoes anything but
    the id itself (model ids are not secrets)."""
    model_id = (model_id or "").strip()
    if not model_id:
        raise ValueError("model is required")
    if not _MODEL_ID_RE.match(model_id):
        raise ValueError(f"invalid model id: {model_id!r}")
    return model_id


def connect(oauth_token, owner_uid=""):
    """Store a freshly pasted `.copilot_token` in RAM; returns a new opaque `credential_id`.

    Raises ValueError if the token is blank -- never logs the token itself, in the exception message
    or anywhere else."""
    oauth_token = (oauth_token or "").strip()
    if not oauth_token:
        raise ValueError("token is required")
    credential_id = _CREDENTIAL_ID_PREFIX + uuid.uuid4().hex
    with _LOCK:
        _STORE[credential_id] = {
            "owner_uid": (owner_uid or "").strip(),
            "oauth_token": oauth_token,
            "service_token": None,
            "service_token_expiry": None,
            "selected_model": None,
            "created_at": _now(),
        }
    return credential_id


def _owner_mismatch(record, owner_uid):
    """True if `owner_uid` was given (not None) and doesn't match the record's owner -- the shared
    fail-closed check every read/write below applies before touching a credential."""
    return owner_uid is not None and record.get("owner_uid", "") != (owner_uid or "").strip()


def disconnect(credential_id, owner_uid=None):
    """Drop a credential from RAM. Idempotent: returns True if something was actually removed,
    False if the id was already gone/unknown -- either way it's gone.

    `owner_uid`: when given (not None), only that credential's own owner may disconnect it -- a
    different browser that merely learned the credential_id gets a no-op False, not someone else's
    credential silently dropped out from under them."""
    credential_id = (credential_id or "").strip()
    if not credential_id:
        return False
    with _LOCK:
        record = _STORE.get(credential_id)
        if record is None:
            return False
        if _owner_mismatch(record, owner_uid):
            return False
        del _STORE[credential_id]
        return True


def resolve(credential_id, owner_uid=None):
    """A COPY of the RAM record for a credential_id, or None if unknown/disconnected/not-yours
    (fails closed -- callers must never fall back to a shared/default endpoint just because this
    returns None).

    `owner_uid`: when given (not None), the record is only returned if it belongs to that owner --
    the same fail-closed check as `disconnect`/`update_service_token`. Pass None (the default) for
    callers that don't have -- or don't need -- an owner context (e.g. a re-resolve deep inside a
    provider that already trusts the request it's serving).

    Returns a copy (not the live dict) so a caller mutating its own local variable can't accidentally
    corrupt the store outside the lock; use `update_service_token` to write back."""
    credential_id = (credential_id or "").strip()
    if not credential_id:
        return None
    with _LOCK:
        record = _STORE.get(credential_id)
        if record is None:
            return None
        if _owner_mismatch(record, owner_uid):
            return None
        return dict(record)


def update_service_token(credential_id, service_token, expiry, owner_uid=None):
    """Cache the derived short-lived Copilot service token (stage 2 of the token exchange) against
    this credential. No-op (returns False) if the credential was disconnected concurrently, or (when
    `owner_uid` is given) doesn't belong to that owner."""
    credential_id = (credential_id or "").strip()
    with _LOCK:
        record = _STORE.get(credential_id)
        if record is None:
            return False
        if _owner_mismatch(record, owner_uid):
            return False
        record["service_token"] = service_token
        record["service_token_expiry"] = expiry
        return True


def set_selected_model(credential_id, model_id, owner_uid=""):
    """Persist the model confirmed by a real probe (see server.py) against this credential.

    Owner-bound: only the browser that connected this credential (matching `owner_uid`) may change
    its model -- raises PermissionError otherwise, so one tester can never repoint another tester's
    credential at a different model. Raises ValueError for a malformed model id. Returns False
    (no-op) if the credential was disconnected concurrently -- fails closed, same as
    `update_service_token`."""
    model_id = _valid_model_id(model_id)
    credential_id = (credential_id or "").strip()
    with _LOCK:
        record = _STORE.get(credential_id)
        if record is None:
            return False
        if record.get("owner_uid") != (owner_uid or "").strip():
            raise PermissionError("credential is not owned by this session")
        record["selected_model"] = model_id
        return True


def describe(credential_id, owner_uid=None):
    """Public view for the UI's 'my LLM' panel -- never includes oauth_token/service_token, only
    whether a credential is connected and (once probed) its confirmed model. `owner_uid` is
    forwarded to `resolve` unchanged (None = no ownership filter)."""
    record = resolve(credential_id, owner_uid=owner_uid)
    if not record:
        return {"connected": False}
    return {"connected": True, "mode": "copilot_token", "created_at": record.get("created_at"),
             "selected_model": record.get("selected_model")}


def is_valid_model_id(model_id):
    """Boolean check for a model id, for callers that just want a yes/no (e.g. a provider validating
    the model it's about to send) rather than a try/except around `_valid_model_id`."""
    try:
        _valid_model_id(model_id)
        return True
    except ValueError:
        return False


def is_credential_id(value):
    """True if `value` has the shape of one of our credential ids (ct_ + 32 lowercase hex chars) --
    NOT a store lookup. Used to decide routing (e.g. server.py keeps a request pinned to the direct
    provider, which then fails closed, when the token merely LOOKS like a credential id but no
    longer resolves -- disconnected or never-existed both fail the same way at the provider)."""
    if not isinstance(value, str) or not value.startswith(_CREDENTIAL_ID_PREFIX):
        return False
    suffix = value[len(_CREDENTIAL_ID_PREFIX):]
    return len(suffix) == 32 and all(ch in "0123456789abcdef" for ch in suffix.lower())


def count():
    """Number of credentials currently held in RAM. Test/introspection helper -- there is no disk
    file to inspect instead, this IS the store."""
    with _LOCK:
        return len(_STORE)
