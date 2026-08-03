"""The browser-facing MCP console: read the surface, and invoke one operation by hand.

Two very different halves, and the difference is the whole design.

**Reading** (`catalog`) touches nothing. It is `mcp_registry` config rendered for a human: which
servers exist, what each is for, which operations are wired, what arguments they take, whose names
those are on the other side. It works with `SDLC_MCP_ENABLED` off, which is the point — an operator
should be able to see what the integration *is* without the ability to fire it.

**Invoking** (`run`) is a second way to reach production, and it is built to be the same way rather
than a new one:

* It takes an ABSTRACT OPERATION NAME, never a tool name. Everything goes through
  `mcp_client.call` -> `mcp_registry.build_call`, so the allow-list and the hard deny baseline apply
  unchanged. There is no argument to this module that can express "call `open_portal_login`".
* Every response passes the SAME exit gate as an evidence packet: redact, then a second sanitizing
  walk that counts what it had to fix. Not conditionally, not by operation type — a manual call to a
  "metadata" operation is redacted exactly like a log read, because `data_class` is our guess about
  their data and a guess must never be load-bearing for exposure. It decides how loudly the panel
  warns; that is all it decides.
* Raw text goes to `incident_raw_store`, the same owner-scoped, TTL'd, capped, purgeable side store
  the investigator's click-through already uses. Not to `chat_sessions.json` — history is replayed
  to the model every turn, so raw production text stored there would be re-read for the life of the
  conversation. The model receives nothing from this module at all; a console call happens outside
  the agent loop, and what the "analyse this" button sends to chat is the REDACTED summary the
  browser already has.
* Endpoints never appear in a result or an error. `mcp_client` already keeps them out of exception
  text; this module adds none back.

What it is FOR: the wiring in `config/mcp_tools.json` is filled in one field at a time from the box,
and until now the only way to find out whether a field was right was to run a whole investigation and
read the refusals. One operation, one form, one answer — including `shape`, which says what their
response actually looked like versus what we declared, is the difference between a config round trip
of minutes and one of days.
"""
import json

from . import config, incident_raw_store, mcp_client, mcp_registry, redaction

# Ceiling on how much of a response is retained for click-through, expressed in lines. The store
# applies its own per-entry line cap on top; this one keeps a pathological single-line response from
# being handed to it whole.
_MAX_RAW_LINES = 2000


class ConsoleDisabled(RuntimeError):
    """`SDLC_MCP_CONSOLE` is off: the panel may list operations but not invoke them."""


def catalog(cfg=None):
    """The MCP surface as a human reads it. Opens no sockets; safe with calling disabled."""
    out = mcp_registry.catalog(cfg)
    out["console_enabled"] = bool(config.MCP_CONSOLE)
    out["raw_retention"] = incident_raw_store.status()
    if not out["calling_enabled"]:
        out["calling_note"] = ("SDLC_MCP_ENABLED is unset — nothing here can be invoked even where "
                               "the config reports `ready`. Readiness is wiring, not permission.")
    elif not out["console_enabled"]:
        out["calling_note"] = ("SDLC_MCP_CONSOLE is off — the chat path can still call these "
                               "operations through the investigator; hand invocation is disabled.")
    return out


def _redact_tree(node, redact, counts):
    """Apply the packet redactor to every string in a decoded body, structure intact.

    Structure is preserved on purpose: `{"lines": [...]}` redacted into one flat blob would hide the
    very shape mismatch this console exists to diagnose.
    """
    if isinstance(node, dict):
        return {str(key): _redact_tree(value, redact, counts) for key, value in node.items()}
    if isinstance(node, list):
        return [_redact_tree(item, redact, counts) for item in node]
    if isinstance(node, str):
        return redact(node, counts)
    return node


def _clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def run(operation, args=None, owner=""):
    """Invoke one allow-listed operation by hand and return a REDACTED result.

    Raises `ConsoleDisabled`; every other failure — unknown operation, denied tool, unwired
    argument, server unreachable, their tool reporting an error — comes back as a result dict with
    `ok: false` and a reason, because in a console those are answers, not crashes, and telling them
    apart is most of what the operator is here to do.
    """
    if not config.MCP_CONSOLE:
        raise ConsoleDisabled(
            "the MCP console is disabled (SDLC_MCP_CONSOLE=0); operations can still be listed")

    # `describe_response` is the only thing still needed from the investigator, and it pulls in the
    # retrieval stack — so it stays a local import, like the one in tools.py, and the console's read
    # half remains usable in a checkout where that stack is not built. The exit gate itself is a
    # top-level import (`redaction`), because it must not be possible to reach `run()` without it.
    from . import incident_investigator as inv

    operation = (operation or "").strip()
    args = {key: value for key, value in (args or {}).items() if value not in (None, "")}

    try:
        out = mcp_client.call(operation, args)
    except (mcp_registry.NotAllowed, mcp_registry.NotWired, mcp_client.Disabled) as exc:
        # A refusal by our own rules. Nothing was sent; say which rule and stop.
        return {"ok": False, "operation": operation, "called": False,
                "refused_by": type(exc).__name__, "error": str(exc)}
    except mcp_client.TransportError as exc:
        # We asked and could not complete the exchange. Distinct from "their tool answered badly" —
        # during an incident those two lead to opposite conclusions.
        return {"ok": False, "operation": operation, "called": True, "transport_failure": True,
                "kind": getattr(exc, "kind", ""), "retryable": getattr(exc, "retryable", False),
                "error": str(exc)}

    counts = {}
    text, text_truncated = _clip(redaction.redact(out.get("text") or "", counts),
                                 config.MCP_CONSOLE_MAX_CHARS)
    structured = _redact_tree(out.get("structured"), redaction.redact, counts)
    # Structured bodies get the same render budget; a 4 MB JSON array is no more renderable than a
    # 4 MB string. Measured after redaction so the check is on what would actually be sent.
    structured_truncated = False
    if structured is not None and len(json.dumps(structured, ensure_ascii=False)) > \
            config.MCP_CONSOLE_MAX_CHARS:
        structured, structured_truncated = None, True

    result = {
        "ok": bool(out.get("ok")),
        "called": True,
        "operation": operation,
        "server": out.get("server") or "",
        "tool": out.get("tool") or "",
        "tool_reported_error": bool(out.get("tool_reported_error")),
        # Argument NAMES we sent. Not the values: the operator typed them, so echoing them back
        # teaches nothing and puts them in one more place that gets screenshotted and pasted.
        "params_sent": list(out.get("params_sent") or []),
        "text": text,
        "structured": structured,
        "truncated": bool(out.get("truncated")) or text_truncated or structured_truncated,
        "response_bytes_capped": bool(out.get("truncated")),
        "non_text_blocks": list(out.get("non_text_blocks") or []),
        "redacted": counts,
        "elapsed_ms": out.get("elapsed_ms", 0),
        "attempts": out.get("attempts", 1),
        "retried": bool(out.get("retried")),
        "retry_reason": out.get("retry_reason") or "",
        "protocol": out.get("protocol") or "",
        "server_info": out.get("server_info") or {},
        "provenance": out.get("provenance") or "",
        # What their body actually looked like next to what we declared. The single most useful thing
        # to paste back from the box when a field mapping is wrong, and it carries no production text.
        "shape": inv.describe_response(out, operation),
        "timezone_warning": out.get("timezone_warning") or "",
    }

    # Click-through to the unredacted original, on exactly the terms the investigator's already has:
    # only when SDLC_INCIDENT_RAW_LOGS is on, only into the owner-scoped store, TTL'd and purgeable.
    raw_text = out.get("text") or ""
    if raw_text and incident_raw_store.enabled():
        result["raw_ref"] = incident_raw_store.put(
            owner, raw_text.splitlines()[:_MAX_RAW_LINES],
            meta={"operation": operation, "server": out.get("server") or "",
                  "tool": out.get("tool") or "", "via": "mcp_console"})
    result["storage_rule"] = (
        "raw retention is ON for the UAT internal test: the unredacted response is in the "
        "owner-scoped side store and this browser can click through to it. It is NOT in chat "
        "history and no model receives it."
        if incident_raw_store.enabled() else
        "raw retention is off: the unredacted response was discarded and no code path returns it.")

    # Defence 2, and the one that is actually a leak test: anything still PII-shaped at the exit is a
    # bug upstream, so it is counted rather than quietly fixed. A silent save is indistinguishable
    # from correct behaviour, which is how a redaction bug survives a demo.
    cleaned, leak = redaction.sanitize_packet(result)
    cleaned["exit_scan"] = leak
    return cleaned
