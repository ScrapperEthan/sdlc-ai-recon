"""`tbl_use_case_channel_rule.traffic_percentage` — reading a configured share as "does this send".

OWNER ANSWER 2026-08-05, two statements that must be kept apart because they are not equally
strong:

1. **`traffic_percentage = 0` means the channel does not send.** Definite. A rule row at 0% is
   configured but idle, and counting it as a live channel overstates blast radius — the same class
   of error as counting a `>` fallback stage as live traffic.

2. **A blank `tbl_use_case_router.vendor` is "基本上" because the percentage is 0.** Explicitly
   *mostly*, not always. So this module never *derives* one from the other. It supplies the check,
   and `usecase_router.router_for_rule` reports per row whether the owner's explanation actually
   holds — the rows where it does NOT are the interesting residue, and they are exactly what would
   be hidden by implementing statement 2 as a rule.

The `sends` tri-state is the whole point: True / False / **None for unknown**. A blank or
unparseable percentage is not 0. Treating "no value" as "no traffic" would silently retire live
channels, which is worse than the fake decoding this codebase keeps stamping out, because it
removes something real rather than adding something false.
"""

# Out-of-range values are reported, never clamped: 150 might mean "150%" (a config error) or a
# mis-typed 15, and picking one would be a guess. Both readings agree it is not zero, so `sends`
# stays True — that much IS derivable — while `in_range` carries the defect.
_MIN, _MAX = 0.0, 100.0


def read(raw):
    """Raw cell -> {raw, value, known, sends, in_range, note}.

    `sends`: True (>0), False (exactly 0), None (blank/unparseable — unknown, NOT zero).
    """
    text = str(raw if raw is not None else "").strip().rstrip("%").strip()
    if not text:
        return {"raw": "", "value": None, "known": False, "sends": None, "in_range": None,
                "note": ("traffic_percentage is blank on this rule — unknown, NOT zero. A blank "
                          "must never be read as 'does not send'.")}
    try:
        value = float(text)
    except ValueError:
        return {"raw": str(raw).strip(), "value": None, "known": False, "sends": None,
                "in_range": None,
                "note": f"traffic_percentage {str(raw).strip()!r} is not a number — unknown, not zero."}

    in_range = _MIN <= value <= _MAX
    out = {"raw": str(raw).strip(), "value": value, "known": True, "sends": value > 0,
           "in_range": in_range, "note": ""}
    if not in_range:
        out["note"] = (f"traffic_percentage {value:g} is outside 0–100 — reported as configured, "
                        "not clamped. It is still non-zero, so the channel does send.")
    elif value == 0:
        out["note"] = ("traffic_percentage is 0 — this channel rule is configured but sends no "
                        "traffic (owner-confirmed 2026-08-05). Do not count it as a live channel.")
    return out


def verdict(rules, channel=""):
    """One aggregate verdict over a set of rule rows that belong together.

    A group sends if ANY row does; it is idle only when EVERY row is provably 0. A group with some
    zeros and some unreadable rows is `None` (unknown), not idle — for the same reason blank != 0
    above: retiring a channel needs positive evidence, and getting this backwards silently deletes
    live traffic from a blast radius.
    """
    entry = {"channel": channel, "rules": 0, "sending": 0, "idle": 0, "unknown": 0}
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        entry["rules"] += 1
        sends = read(rule.get("traffic_percentage")).get("sends")
        entry["sending" if sends is True else ("idle" if sends is False else "unknown")] += 1
    if entry["sending"]:
        entry["sends"] = True
    elif entry["rules"] and not entry["unknown"]:
        entry["sends"] = False
    else:
        entry["sends"] = None
    return entry


def summarise(rules, key=None):
    """Rule rows grouped into sending / idle / unknown, by channel.

    Per channel rather than per row because that is the unit a blast-radius answer is given in.
    `key` lets the caller supply its own channel normaliser (delivery_chain passes the canonical
    one, so `PUSH+INBOX` and `PUSH_INBOX` do not come back as two half-answers).
    """
    key = key or (lambda name: (name or "").strip())
    groups = {}
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        channel = key(rule.get("channel"))
        if not channel:
            continue
        groups.setdefault(channel, []).append(rule)
    return [verdict(rows, channel) for channel, rows in
            sorted(groups.items(), key=lambda item: item[0].lower())]


def idle_channels(rules, key=None):
    """Channels whose every rule row is at 0% — configured, but carrying nothing."""
    return [entry["channel"] for entry in summarise(rules, key) if entry["sends"] is False]
