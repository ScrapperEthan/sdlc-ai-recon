"""Per-user LLM route registry (BYO-LLM).

Each user runs their own local LLM (copilot-api) and exposes it to the server through a reverse
tunnel, so on the server their endpoint is a loopback port (``127.0.0.1:<their-port>``). A user
registers that endpoint once and gets a token; the browser sends the token on every request and the
server routes that user's LLM calls to their own endpoint (see ``config.set_llm_override``).

JSON-backed (``webapp_data/llm_routes.json``, gitignored like the session store), stdlib only,
atomic writes under a lock. The loopback guard is the security boundary: a registered endpoint must
resolve to loopback, which both matches the tunnel model and blocks SSRF to arbitrary internal hosts.
"""
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import atomic_json, config

_LOCK = threading.Lock()
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load():
    if not os.path.exists(config.LLM_ROUTES_STORE):
        return {}
    with open(config.LLM_ROUTES_STORE, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _save(data):
    atomic_json.write_json(config.LLM_ROUTES_STORE, data)


def validate_base_url(base_url):
    """Return a normalized base_url or raise ValueError. Enforces http(s) + loopback host (unless
    the deployment opted into non-loopback connector hosts).

    Validation only -- does not register or persist anything. Public so server.py can validate a
    candidate endpoint (e.g. before probing it or listing its models) without registering a route."""
    base_url = (base_url or "").strip()
    if not base_url:
        raise ValueError("base_url is required")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must be http(s)")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("base_url has no host")
    if not config.LLM_ALLOW_NONLOOPBACK and host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"base_url host must be loopback ({', '.join(sorted(_LOOPBACK_HOSTS))}); got {host!r}. "
            "Expose your local LLM to the server via a reverse tunnel and register its 127.0.0.1 port."
        )
    return base_url.rstrip("/")


def _open_models_request(req, timeout):
    """Thin indirection over `urllib.request.urlopen` -- exists so tests can stub the tunnel's
    `/models` response (`mock.patch.object(llm_routes, "_open_models_request", ...)`) without
    patching the shared `urllib.request` module itself, which would also intercept unrelated
    urlopen calls elsewhere in the same process (e.g. a test's own HTTP client)."""
    return urllib.request.urlopen(req, timeout=timeout)


def list_remote_models(base_url, api_key=""):
    """GET `<base_url>/models` (the same OpenAI-compatible discovery endpoint the provider files
    talk to) and return only `{"models": [{"id", "label"}, ...]}` -- for the tunnel panel's 'Load
    models' button, used BEFORE a tunnel is registered. Read-only: never touches
    `llm_routes.json`, never registers a route.

    Raises ValueError for a bad base_url (message is safe to return as-is -- purely local
    validation, no network involved) or RuntimeError for anything network/response related
    (message is deliberately generic -- callers must not relay upstream response bodies)."""
    base_url = validate_base_url(base_url)
    req = urllib.request.Request(base_url + "/models", method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with _open_models_request(req, 10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise RuntimeError("tunnel endpoint rejected the model list request") from None
    except urllib.error.URLError:
        raise RuntimeError("tunnel endpoint unreachable") from None
    except (ValueError, OSError):
        raise RuntimeError("tunnel endpoint returned an invalid response") from None

    entries = body.get("data") if isinstance(body, dict) else body
    if entries is None and isinstance(body, dict):
        entries = body.get("models")  # some endpoints answer {"models": [...]} instead of {"data": [...]}
    if not isinstance(entries, list):
        entries = []
    models = []
    for entry in entries:
        if isinstance(entry, str):
            models.append({"id": entry, "label": entry})
        elif isinstance(entry, dict) and entry.get("id"):
            model_id = str(entry["id"])
            models.append({"id": model_id, "label": str(entry.get("label") or model_id)})
    return {"models": models}


def register(base_url, model="", api_key="", label="", provider="", token=None):
    """Register (or update) a user's LLM endpoint; returns the public record incl. its token."""
    base_url = validate_base_url(base_url)
    with _LOCK:
        data = _load()
        token = (token or "").strip() or uuid.uuid4().hex
        existing = data.get(token) or {}
        record = {
            "token": token,
            "base_url": base_url,
            "model": (model or "").strip(),
            "api_key": (api_key or "").strip(),
            "provider": (provider or "").strip() or existing.get("provider") or "",
            "label": (label or "").strip() or existing.get("label") or "user",
            "created_at": existing.get("created_at") or _now(),
            "updated_at": _now(),
        }
        data[token] = record
        _save(data)
    return _public(record)


def resolve(token):
    """The override dict (base_url/api_key/model/provider) for a token, or None. Used by the server
    to bind a request to the user's endpoint via config.set_llm_override. Empty fields fall back to
    env -- notably `provider`, so a route registered before this field existed still resolves fine."""
    token = (token or "").strip()
    if not token:
        return None
    with _LOCK:
        record = _load().get(token)
    if not record:
        return None
    override = {"base_url": record.get("base_url") or ""}
    if record.get("model"):
        override["model"] = record["model"]
    if record.get("api_key"):
        override["api_key"] = record["api_key"]
    if record.get("provider"):
        override["provider"] = record["provider"]
    return override


def _public(record):
    """Record without the secret (api_key) or full internals — safe to hand back to the browser."""
    return {
        "token": record.get("token"),
        "label": record.get("label"),
        "base_url": record.get("base_url"),
        "model": record.get("model") or "",
        "has_api_key": bool(record.get("api_key")),
        "updated_at": record.get("updated_at"),
    }


def describe(token):
    """Public view of a token's route, or {'registered': False} — for the UI's 'my LLM' panel."""
    token = (token or "").strip()
    if not token:
        return {"registered": False}
    with _LOCK:
        record = _load().get(token)
    if not record:
        return {"registered": False}
    return {"registered": True, **_public(record)}
