"""What we are allowed to call on the colleagues' MCP servers, and what it is called over there.

The seam: this side owns the ABSTRACT operations (`log.read`, `aws.alarm_history`, …) and the code
refers only to those. `config/mcp_tools.json` — intranet-owned — says which real tool each one maps
to and what that tool's parameters are actually named. So a rename on their side is a config edit
on the box, not an engine change waiting on external development. RUNBOOK-49/50/51 were three
separate production fixes that all turned out to be edits to one hard-coded vocabulary; this is
that lesson applied before the fact.

Three properties worth being explicit about:

* **Allow-list, not discovery.** Only operations declared in the config may be called. `tools/list`
  is for finding and cross-checking names — a tool appearing there gains no call rights, and MCP
  annotations are hints from a remote server, never an authorization decision.
* **Hard deny beats configuration.** Anything matching `never_expose` is refused even if someone
  declares it, because the damage from calling a resend/submit tool is real and irreversible. The
  assistant is read-only on production; that is not a setting.
* **Unwired fails closed and says so.** A `"?"` placeholder means "not filled in yet". Calling such
  an operation raises with the exact list of missing fields, so the assistant reports "this is not
  wired up yet" instead of guessing a parameter name and silently querying the wrong thing.
"""
import fnmatch
import json
import os

from retriever import config as retriever_config

UNSET = "?"


class NotWired(RuntimeError):
    """An operation exists but its config still has placeholders."""


class NotAllowed(RuntimeError):
    """The operation is unknown, disabled, or hard-denied."""


def _config_path():
    return os.environ.get("SDLC_MCP_TOOLS") or os.path.join(
        retriever_config.ROOT, "config", "mcp_tools.json")


def load():
    try:
        with open(_config_path(), encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {"servers": {}, "operations": {}, "never_expose": {}}
    return payload if isinstance(payload, dict) else {"servers": {}, "operations": {}}


def _clean(mapping):
    """Config dicts carry `_note`/`_README` documentation keys; they are never data."""
    if not isinstance(mapping, dict):
        return {}
    return {k: v for k, v in mapping.items() if not str(k).startswith("_")}


def servers(cfg=None):
    return _clean((cfg or load()).get("servers"))


def server_url(name, cfg=None):
    """Addresses live in env vars, never in git — the config only names which var to read."""
    spec = servers(cfg).get(name) or {}
    return os.environ.get(spec.get("url_env") or "", "")


def operations(cfg=None):
    return _clean((cfg or load()).get("operations"))


def _denied(tool, cfg):
    deny = (cfg.get("never_expose") or {})
    name = (tool or "").lower()
    if name in {str(t).lower() for t in (deny.get("tools") or [])}:
        return True
    return any(fnmatch.fnmatch(name, str(p).lower()) for p in (deny.get("patterns") or []))


def _missing(spec):
    missing = []
    if spec.get("tool", UNSET) == UNSET:
        missing.append("tool")
    for ours, theirs in _clean(spec.get("args")).items():
        if theirs == UNSET:
            missing.append(f"args.{ours}")
    return missing


def readiness(cfg=None):
    """Per-operation wiring status — what the /api/mcp/status endpoint and the box report read.

    Deliberately reports `blocked` separately from `unwired`: "nobody has filled this in" and "this
    must never be called" are different situations and should never be confused for each other."""
    cfg = cfg or load()
    server_specs = servers(cfg)
    out = {}
    for name, spec in operations(cfg).items():
        if not isinstance(spec, dict):
            continue
        server = spec.get("server") or ""
        tool = spec.get("tool") or ""
        missing = _missing(spec)
        if _denied(tool, cfg):
            state = "blocked"
        elif not server_specs.get(server, {}).get("enabled"):
            state = "disabled"
        elif spec.get("tool", UNSET) == UNSET:
            state = "unwired"          # no tool name: nothing about it is callable
        elif missing:
            state = "partial"          # callable, but not with the arguments still marked "?"
        else:
            state = "ready"
        out[name] = {"state": state, "server": server, "tool": tool, "missing": missing,
                     "endpoint_configured": bool(server_url(server, cfg))}
    return out


def summary(cfg=None):
    states = readiness(cfg)
    counts = {}
    for entry in states.values():
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return {
        "operations": len(states),
        "by_state": counts,
        "ready": sorted(n for n, e in states.items() if e["state"] == "ready"),
        "servers": {name: {"enabled": bool(spec.get("enabled")),
                           "transport": spec.get("transport") or "",
                           "endpoint_configured": bool(server_url(name, cfg))}
                    for name, spec in servers(cfg).items()},
        "note": ("`unwired` means config/mcp_tools.json still has \"?\" placeholders — the intranet "
                 "side fills those from a live tools/list. Nothing is guessed."),
    }


def build_call(operation, args=None, cfg=None):
    """Translate one abstract call into (server, tool, their_params).

    Raises rather than improvising: an incident answer built on a wrongly-named parameter is worse
    than an answer that says the integration is not ready."""
    cfg = cfg or load()
    spec = operations(cfg).get(operation)
    if not isinstance(spec, dict):
        raise NotAllowed(
            f"unknown MCP operation: {operation!r}. Only operations declared in "
            f"config/mcp_tools.json may be called; discovery via tools/list grants no access.")

    tool = spec.get("tool") or ""
    if _denied(tool, cfg):
        raise NotAllowed(
            f"operation {operation!r} maps to {tool!r}, which is on the never_expose deny list. "
            f"The assistant is read-only on production; action-taking tools are never callable.")

    server = spec.get("server") or ""
    if not servers(cfg).get(server, {}).get("enabled"):
        raise NotAllowed(f"MCP server {server!r} is not enabled in config/mcp_tools.json")

    if spec.get("tool", UNSET) == UNSET:
        raise NotWired(
            f"operation {operation!r} has no tool name yet — fill `tool` in config/mcp_tools.json "
            f"from a live tools/list on the box.")

    # Only the arguments actually being passed need a mapping. An unfilled placeholder for an
    # argument nobody is using is irrelevant, so partial wiring is usable for the parts that ARE
    # wired — the intranet side can fill this in one field at a time and each one works as it lands.
    params = dict(_clean(spec.get("const")))
    arg_map = _clean(spec.get("args"))
    for ours, value in (args or {}).items():
        if value is None:
            continue     # an unsupplied optional argument is simply not sent
        theirs = arg_map.get(ours)
        if theirs is None:
            raise NotWired(
                f"operation {operation!r} has no mapping for argument {ours!r}; add it to "
                f"config/mcp_tools.json args.")
        if theirs == UNSET:
            raise NotWired(
                f"operation {operation!r} cannot pass {ours!r} yet — its parameter name is still "
                f"\"?\" in config/mcp_tools.json. Fill it from a live tools/list on the box.")
        params[theirs] = value
    return server, tool, params
