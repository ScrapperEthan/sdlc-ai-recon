"""The exit gate: nothing leaves this process carrying customer data.

Lifted out of `incident_investigator` once it acquired a second caller. It was never really an
investigator detail — it is the single boundary every path from a production system to a browser or
a model has to cross, and a boundary that lives inside one of the things it bounds is one somebody
eventually routes around.

Two defences, deliberately not one:

* `redact` masks on the way in, at the point the text is read.
* `sanitize_packet` walks the finished structure on the way out and COUNTS what it still had to fix.
  Reaching it should mean a bug upstream, so it reports rather than repairing quietly — a silent
  save is indistinguishable from correct behaviour, which is how a redaction bug survives a demo.

Callers: `incident_investigator` (evidence packets) and `mcp_console` (hand-invoked MCP responses).
Both re-export nothing and import from here, so there is one gate, not two implementations that
drift apart.
"""
import hashlib
import re


# Each pattern keeps a short stable digest of what it replaced, so the same account appearing on
# five lines is still recognisably the same account without the value ever being exposed. A plain
# `***` would destroy the correlation that makes a log excerpt worth reading at all.
_PII = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # +852 / 8-digit HK mobiles / any 7+ run that is punctuated like a phone number.
    ("phone", re.compile(r"(?<!\d)(?:\+?852[-\s]?)?\d{4}[-\s]?\d{4}(?!\d)")),
    # Card/account-like: 12-19 digits, optionally grouped. Anchored to end ON a digit so it does not
    # swallow the following separator and glue two words together in the excerpt.
    ("account", re.compile(r"(?<!\d)\d(?:[ -]?\d){11,18}(?!\d)")),
    # MDC tracking ids and similar long opaque handles: they are customer-linkable, but dropping
    # them entirely would make it impossible to see that 40 lines concern ONE message. The trailing
    # group is greedy so the WHOLE handle is replaced — matching only its first half would leave a
    # fragment behind and defeat the point. Each group needs 4+ chars, which keeps `2026-07-30` and
    # `AWS-HK-SNS` out of scope.
    ("tracking", re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")),
    ("hkid", re.compile(r"\b[A-Z]{1,2}\d{6}\(?[0-9A]\)?\b")),
)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:6]


def redact(text, counts=None):
    """Text with every PII-shaped run replaced by `<kind:digest>`. Idempotent."""
    if not text:
        return ""
    out = text
    for kind, pattern in _PII:
        def _sub(match, kind=kind):
            if counts is not None:
                counts[kind] = counts.get(kind, 0) + 1
            return f"<{kind}:{_digest(match.group(0))}>"
        out = pattern.sub(_sub, out)
    return out


def _residual_pii(text):
    """Which patterns still match — the exit check. Our own markers must not re-trigger it."""
    stripped = re.sub(r"<\w+:[0-9a-f]{6}>", " ", text or "")
    return sorted({kind for kind, pattern in _PII if pattern.search(stripped)})


# Machine-generated identifiers, exempt from the exit scan. `uuid4().hex` frequently contains eight
# consecutive digits, which the account/phone patterns match — so without this the gate ate roughly
# one in eight `raw_ref`s, silently breaking click-through for that evidence item AND inflating
# `sanitized_at_exit`, the counter whose whole job is to signal a REAL upstream redaction bug. A
# false positive in a leak detector is not harmless; it trains you to ignore it.
#
# Safe because these values never come from log content: `raw_ref` is set only from
# `incident_raw_store.put()`, and app/source/file names come from the server's own listings by way of
# our selection. A dated log file (`otx_trace.log.20260701`) is eight consecutive digits and matches
# the phone pattern, so without this the gate mangles the one field an operator needs to go look at
# the file themselves. Do not add a key here that could carry log-derived text.
_IDENTIFIER_KEYS = frozenset({"raw_ref", "file", "app", "source"})


def sanitize_packet(node, report=None):
    """Walk a finished packet and blank any string that still looks like PII.

    Defence 2. Reaching this should mean a bug upstream, so it counts what it had to do rather than
    fixing it quietly: a silent save is indistinguishable from correct behaviour.
    """
    report = report if report is not None else {"sanitized_at_exit": 0, "kinds": []}
    if isinstance(node, dict):
        return {key: (value if key in _IDENTIFIER_KEYS and isinstance(value, str)
                      else sanitize_packet(value, report)[0])
                for key, value in node.items()}, report
    if isinstance(node, list):
        cleaned = []
        for item in node:
            value, _ = sanitize_packet(item, report)
            cleaned.append(value)
        return cleaned, report
    if isinstance(node, str):
        kinds = _residual_pii(node)
        if kinds:
            report["sanitized_at_exit"] += 1
            report["kinds"] = sorted(set(report["kinds"]) | set(kinds))
            # Mask the matching span rather than discarding the whole string. Blanking it protected
            # the data but destroyed the message around it — and most of these strings are prose WE
            # composed ("the query budget ran out, so these were never tried: ..."), where losing the
            # sentence costs an operator the reason and gains nothing. `redact` is the same masking
            # used upstream, so a genuine leak is still neutralised here.
            return redact(node), report
        return node, report
    return node, report
