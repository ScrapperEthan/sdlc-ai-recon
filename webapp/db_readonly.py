"""The read-only UAT database path: run a named query through the intranet's skill, and make sure
nothing leaves this process that should not.

The connection is NOT ours. The read-only DB skill belongs to the intranet, holds the token
provider, is excluded from git on their box and must never be committed here — so this module names
none of it. It imports the runner from a path given by an environment variable and calls exactly the
two functions their handoff specifies (RUNBOOK-72 §1):

    check_readonly_connection(environment="u")
    run_readonly_query(sql, params=None, limit=100, environment="u")

so `psycopg` is never imported by our core, which stays standard-library only.

What this module adds on top of their runner — which already enforces its own read-only rules and is
the layer that actually protects the database:

* **We do not send what we would not accept back.** `db_registry.statement_problems` runs when the
  query is built and again immediately before the call. A request only their side refuses is a
  request we should not have made.
* **Zero calls when there is nothing to ask.** An unknown, unwired, disabled or out-of-policy query
  costs no import and no connection. This is RUNBOOK-71's lesson moved one system across: the hole
  there was a metadata call that ran before the per-target checks, and it was invisible precisely
  because it looked like preparation rather than a query.
* **Column allow-list before redaction, not instead of it.** Only columns named in the config reach
  the packet; whatever survives that still crosses `redaction.sanitize_packet`. Pattern matching
  recognises a phone number but cannot recognise "this column is a customer name" — so the column
  the config never lists is the one that is actually safe.
* **"Not ready" is never "no rows".** A failed connection, an unreadable response shape and an empty
  result set are three different facts. The first two return a packet with NO `rows` key at all, so
  there is no empty list for a reader to mistake for evidence of absence. That mistake — a refusal
  read as an answer — is the exact shape of the LogDream keyword P0.
* **One environment, and it is not a parameter.** UAT comes from config and is checked against an
  allow-list. There is no retry, no fallback account and no way for a caller to name a database.
"""
import datetime
import importlib.util
import os
import re
import sys

from . import config
from . import db_registry
from . import redaction

# UAT is not production, and a database row reads as authoritative in a way a log line does not.
# Every packet carries this, so it survives being quoted out of context.
CAVEAT = ("Live UAT read-only query. UAT is NOT production: it may hold copied, masked or synthetic "
          "data. Never present a UAT row as a production fact, and never conclude a record does not "
          "exist in production because it is absent here.")

# What a caller must not conclude from a non-ok packet. Carried in the packet rather than left to
# the prompt, because the failure it guards against is a model narrating a refusal as a finding.
NOT_A_RESULT = ("This is NOT an empty result set. The query did not run. Say the database is not "
                "connected/wired — never that no such record exists.")

_MODULE_CACHE = {}


class NotReady(RuntimeError):
    """The path exists but cannot be used right now — disabled, absent skill, unreadable response."""


def _safe_reason(exc):
    """An exception's text, stripped of anything that identifies the database, then bounded.

    Their runner is careful not to echo connection details; ours is the layer that would carry them
    into a browser or a model prompt if it ever did, and a stack trace is exactly where a DSN turns
    up. Host, credentials and URLs go before the text is used anywhere.
    """
    text = str(exc or "")
    text = re.sub(r"(?i)\b(password|pwd|token|secret|user|username|host|hostaddr|port|dbname|dsn)"
                  r"\s*=\s*\S+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)\b[a-z+]+://\S+", "<redacted-url>", text)
    text = re.sub(r"\b[\w.-]+\.(?:amazonaws\.com|rds\.amazonaws\.com|hsbc\.com|local)\b",
                  "<redacted-host>", text)
    text = redaction.redact(text)
    return text[:300]


def _runner_path(cfg=None):
    """Where the intranet's skill lives on this box. Never in git, never in the config file."""
    spec = db_registry.runner(cfg)
    var = str(spec.get("module_env") or "SDLC_DB_SKILL")
    raw = (os.environ.get(var) or "").strip()
    if not raw:
        raise NotReady(f"{var} is not set: the read-only DB skill's path is unknown. It is the "
                       f"intranet's skill and lives only on the box — nothing to import here.")
    path = raw
    if os.path.isdir(path):
        path = os.path.join(path, "scripts", "readonly_db.py")
    if not os.path.isfile(path):
        raise NotReady(f"{var} points at something that is not a readable runner module.")
    return path


def _load_runner(cfg=None):
    """Import the intranet runner from its own path. Cached on (path, mtime)."""
    path = _runner_path(cfg)
    try:
        key = (path, os.path.getmtime(path))
    except OSError as exc:
        raise NotReady(f"cannot stat the DB runner: {_safe_reason(exc)}") from None
    if key in _MODULE_CACHE:
        return _MODULE_CACHE[key]
    try:
        spec = importlib.util.spec_from_file_location("sdlc_intranet_readonly_db", path)
        module = importlib.util.module_from_spec(spec)
        # Registered before exec so a runner that imports itself by name resolves; removed again on
        # failure so a half-initialised module is never reused.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except BaseException as exc:                       # noqa: BLE001 - a bad import must not escape
        sys.modules.pop("sdlc_intranet_readonly_db", None)
        raise NotReady(f"the DB runner could not be imported: {_safe_reason(exc)}") from None
    _MODULE_CACHE.clear()
    _MODULE_CACHE[key] = module
    return module


def _callable(module, cfg, key, default):
    name = str(db_registry.runner(cfg).get(key) or default)
    func = getattr(module, name, None)
    if not callable(func):
        raise NotReady(f"the DB runner has no callable {name!r} — set runner.{key} in "
                       f"config/db_queries.json to whatever it is actually called.")
    return func


def _extract(raw, cfg):
    """Their response -> (columns, rows-as-dicts). Fail closed on a shape we cannot read.

    RUNBOOK-61 was this exact failure one system over: their responses were structured, we read them
    as text, and two entries became eleven inside a VERIFICATION set. So the mapping lives in
    `runner.response` in the config — and when it is unset we accept only the two shapes that cannot
    be misread, rather than guessing at a third.
    """
    response = db_registry._clean(db_registry.runner(cfg).get("response"))
    rows_key = response.get("rows", db_registry.UNSET)
    cols_key = response.get("columns", db_registry.UNSET)
    row_format = str(response.get("row_format", db_registry.UNSET))

    if rows_key != db_registry.UNSET:
        if not isinstance(raw, dict) or rows_key not in raw:
            raise NotReady(f"runner.response.rows is {rows_key!r} but the response has no such key. "
                           f"Correct it in config/db_queries.json; nothing was read.")
        rows = raw.get(rows_key)
        columns = raw.get(cols_key) if cols_key != db_registry.UNSET else None
        if not isinstance(rows, list):
            raise NotReady(f"runner.response.rows points at a {type(rows).__name__}, not a list.")
        if row_format == "sequence" or (row_format == db_registry.UNSET
                                        and rows and not isinstance(rows[0], dict)):
            if not isinstance(columns, list) or not columns:
                raise NotReady("row_format is a sequence but runner.response.columns does not "
                               "resolve to a list of column names.")
            names = [str(c) for c in columns]
            return names, [dict(zip(names, row)) for row in rows]
        dicts = [r for r in rows if isinstance(r, dict)]
        if len(dicts) != len(rows):
            raise NotReady("the response mixes dict and non-dict rows.")
        names = [str(c) for c in columns] if isinstance(columns, list) and columns else _keys(dicts)
        return names, dicts

    # Unmapped: only the two unambiguous shapes.
    if isinstance(raw, list) and all(isinstance(item, dict) for item in raw):
        return _keys(raw), list(raw)
    if isinstance(raw, dict) and isinstance(raw.get("rows"), list) \
            and isinstance(raw.get("columns"), list) and raw.get("columns"):
        names = [str(c) for c in raw["columns"]]
        rows = raw["rows"]
        if all(isinstance(item, dict) for item in rows):
            return names, list(rows)
        return names, [dict(zip(names, row)) for row in rows]
    raise NotReady(
        f"the runner returned a {type(raw).__name__} this side cannot read without guessing. Fill "
        f"runner.response (rows / columns / row_format) in config/db_queries.json with the real "
        f"shape. No rows were read and this is NOT an empty result.")


def _keys(rows):
    names = []
    for row in rows:
        for key in row:
            if key not in names:
                names.append(str(key))
    return names


def _project(columns_allowed, columns_seen, rows):
    """Keep only allow-listed columns, in the config's order. Dropping is the safe direction."""
    allowed = [c for c in columns_allowed]
    seen = {str(c) for c in columns_seen}
    for row in rows:
        seen |= {str(k) for k in row}
    kept = [c for c in allowed if c in seen]
    dropped = sorted(seen - set(allowed))
    missing = [c for c in allowed if c not in seen]
    cap = max(200, int(getattr(config, "TOOL_STRING_CAP", 4000)))
    out = []
    for row in rows:
        clean = {}
        for name in kept:
            value = row.get(name)
            if isinstance(value, str) and len(value) > cap:
                value = value[:cap] + f"… [{len(value) - cap} more chars]"
            elif not isinstance(value, (str, int, float, bool, type(None))):
                # Dates, decimals and anything else the driver hands back: rendered, bounded, and
                # never trusted to serialise on its own further down the pipe.
                value = str(value)[:cap]
            clean[name] = value
        out.append(clean)
    return kept, dropped, missing, out


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refusal(name, state, reason, **extra):
    """A packet with NO `rows` key. There is no empty list here to mistake for an answer."""
    packet = {
        "ok": False,
        "state": state,
        "query": name,
        "reason": reason,
        "means_no_data": False,
        "hint": NOT_A_RESULT,
        "environment": "uat",
        "production_verified": False,
        "queried_at": _now(),
    }
    packet.update(extra)
    return packet


def catalog(probe=False):
    """What is wired, without touching the database. `probe=True` additionally tests the connection.

    Contacting nothing is the point: an assistant asked "what can you query" must be able to answer
    from the config alone, or every catalog listing costs a connection.
    """
    cfg = db_registry.load()
    out = {
        "ok": True,
        "state": "catalog",
        "environment": "uat",
        "production_verified": False,
        "calling_enabled": bool(config.DB_ENABLED),
        "skill_configured": _skill_configured(cfg),
        "queries": db_registry.readiness(cfg),
        "config_path": db_registry._config_path(),
        "config_error": cfg.get("_load_error", ""),
        "caveat": CAVEAT,
    }
    ready = [n for n, e in out["queries"].items() if e["state"] == "ready"]
    out["ready"] = sorted(ready)
    out["note"] = ("No named query is wired yet — the intranet fills the real SQL, schema, table and "
                   "column names into config/db_queries.json. Until then the database cannot be "
                   "queried at all, and that is not the same as it holding no data."
                   if not ready else "")
    if probe:
        out["connection"] = check()
    return out


def _skill_configured(cfg=None):
    try:
        _runner_path(cfg)
        return True
    except NotReady:
        return False


def check():
    """Ask the runner whether the connection works. Returns a verdict, never connection details."""
    if not config.DB_ENABLED:
        return {"ok": False, "state": "disabled",
                "reason": "SDLC_DB_ENABLED is not set: the read-only database path is off."}
    try:
        module = _load_runner()
        func = _callable(module, db_registry.load(), "check_function", "check_readonly_connection")
        result = func(environment=db_registry.environment())
    except NotReady as exc:
        return {"ok": False, "state": "not_ready", "reason": str(exc)}
    except db_registry.NotAllowed as exc:
        return {"ok": False, "state": "refused", "reason": str(exc)}
    except BaseException as exc:                       # noqa: BLE001
        return {"ok": False, "state": "error", "reason": _safe_reason(exc)}
    # Their check's return shape is not pinned down yet (RUNBOOK-73 asks for it). A dict is read by
    # its `ok`; anything else is read by truthiness. Neither reading invents a success.
    ok = bool(result.get("ok")) if isinstance(result, dict) else bool(result)
    return {"ok": ok, "state": "ok" if ok else "error",
            "reason": "" if ok else "the runner reported the connection is not usable",
            "environment": "uat"}


def run(name, args=None, caller="product"):
    """Run one named query. Returns a packet; raises nothing.

    Order matters and is deliberate: the plan is built first (pure, no I/O), so an unwired or
    out-of-policy query produces a precise refusal and costs zero database contact even when the
    path is fully enabled.
    """
    try:
        plan = db_registry.build_query(name, args, caller=caller)
    except db_registry.NotWired as exc:
        return _refusal(name, "not_wired", str(exc))
    except db_registry.NotAllowed as exc:
        return _refusal(name, "refused", str(exc))

    if not config.DB_ENABLED:
        return _refusal(name, "disabled",
                        "SDLC_DB_ENABLED is not set: the read-only database path is off on this "
                        "deployment.")

    try:
        # The second run of the ONE gate definition, immediately before the call. `build_query`
        # already ran it; this is not redundancy for its own sake — `plan` is data, and a caller
        # that assembled one by other means must not get a socket out of it.
        db_registry.check_statement(plan["sql"])
        cfg = db_registry.load()
        module = _load_runner(cfg)
        query = _callable(module, cfg, "query_function", "run_readonly_query")
        raw = query(plan["sql"], params=plan["params"], limit=plan["max_rows"],
                    environment=plan["environment"])
        columns_seen, rows = _extract(raw, cfg)
    except NotReady as exc:
        return _refusal(name, "not_ready", str(exc))
    except db_registry.NotAllowed as exc:
        return _refusal(name, "refused", str(exc))
    except BaseException as exc:                       # noqa: BLE001 - never becomes "no rows"
        # Their runner refusing, the proxy rejecting the role, a timeout: all of them are "we did
        # not get an answer". There is no second attempt with another account or another
        # environment — a fallback that widens access on failure is how a read-only guarantee dies.
        return _refusal(name, "error", _safe_reason(exc))

    truncated = len(rows) > plan["max_rows"]
    rows = rows[:plan["max_rows"]]
    kept, dropped, missing, projected = _project(plan["columns"], columns_seen, rows)
    packet = {
        "ok": True,
        "state": "ok",
        "query": name,
        "columns": kept,
        "rows": projected,
        "row_count": len(projected),
        "truncated": truncated,
        "columns_dropped": dropped,
        "columns_missing": missing,
        "source_tables": plan["source_tables"],
        "environment": "uat",
        "production_verified": False,
        "queried_at": _now(),
        "provenance": f"db:uat/{name}",
        "caveat": CAVEAT,
    }
    # The same exit gate every production-to-browser path crosses. Reaching it with something to fix
    # means the column allow-list let a PII column through, so it counts rather than repairing
    # quietly — see redaction.sanitize_packet.
    packet, report = redaction.sanitize_packet(packet)
    packet["sanitized_at_exit"] = report["sanitized_at_exit"]
    packet["redaction_kinds"] = report["kinds"]
    return packet
