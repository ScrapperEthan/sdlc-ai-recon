"""`tbl_use_case_channel_rule.traffic_percentage` — a configured share, and what 0 actually means.

## 0% is STANDBY, not "off" — corrected 2026-08-06

The first version of this module read `traffic_percentage = 0` as "this channel does not send, do
not count it as live". That is **wrong in the direction that matters most**, and the routing rules
the messaging team wrote down say why:

> if message is high-risk, choose dual vendor with HTCL and CSL,
> **primary 100% HTCL & 0% for CSL** (traffic percentage)

> if need to send to CN, choose LX **(not yet ready)** and CM routers,
> **primary 100% CM & 0% for LX**

A 0% row is a **deliberately provisioned second carrier**. CSL at 0% is precisely who takes over
when HTCL fails — so answering "the HTCL vendor is down, who is affected / what takes over" by
*excluding* the 0% rows deletes the only answer. That is worse than over-counting.

Three things produce a 0%, and this column **cannot tell them apart**:

  * a dual-vendor standby that is ready and waiting (CSL),
  * a carrier registered but **not yet ready** (LX),
  * a route genuinely retired.

So `sends` answers exactly one narrow question — *is this row carrying traffic right now* — and
callers must not promote that into "this channel is irrelevant". `standby` is the flag for the
other reading, and `delivery_chain` keeps 0% channels in the chain rather than dropping them.

This is the SAME shape as `rule_text`'s `>` fallback stages (see `rule_text._STAGE_TRANSITIONS`):
something that is idle in steady state and is the whole answer during an outage. Two questions,
two different correct answers, and the engine must not collapse them.

## The other owner statement, deliberately weaker

**A blank `tbl_use_case_router.vendor` is "基本上" because the percentage is 0.** Explicitly
*mostly*, not always — and measured on the real UAT export it holds for only ~54% of the decidable
rows. So this module never *derives* one from the other; `usecase_router.router_for_rule` checks it
per row. Per the owner 2026-08-06 the rows where it does not hold are **NOT a data-quality
exception** — the routing rules above show several legitimate ways a live route ends up with no
authoritative carrier recorded. They are counted and shown, not flagged.

The `sends` tri-state is the whole point: True / False / **None for unknown**. A blank or
unparseable percentage is not 0. Treating "no value" as "no traffic" would silently retire live
channels, which is worse than the fake decoding this codebase keeps stamping out, because it
removes something real rather than adding something false.
"""

# Out-of-range values are reported, never clamped: 150 might mean "150%" (a config error) or a
# mis-typed 15, and picking one would be a guess. Both readings agree it is not zero, so `sends`
# stays True — that much IS derivable — while `in_range` carries the defect.
_MIN, _MAX = 0.0, 100.0


STANDBY_NOTE = (
    "traffic_percentage is 0 — this route is provisioned but not carrying traffic RIGHT NOW. That "
    "is not the same as 'off': the messaging team's routing rules deliberately create 0% rows as "
    "the second carrier of a dual-vendor pair (high-risk SMS is primary 100% HTCL & 0% CSL; CN "
    "traffic is primary 100% CM & 0% LX). Such a row is exactly what takes over when the primary "
    "fails, so an outage or blast-radius answer MUST include it. A 0% can also mean a carrier "
    "registered but not yet ready, or a genuinely retired route — this column cannot tell the "
    "three apart, so do not assert which one it is.")


def read(raw):
    """Raw cell -> {raw, value, known, sends, standby, in_range, note}.

    `sends`: True (>0), False (exactly 0), None (blank/unparseable — unknown, NOT zero).
    `standby`: True exactly when `sends` is False. A separate name because the two readings drive
    opposite behaviour — `sends` answers "is it carrying traffic now", `standby` answers "is it
    provisioned to take over", and only the first may be used to narrow a live-channel list.
    """
    text = str(raw if raw is not None else "").strip().rstrip("%").strip()
    if not text:
        return {"raw": "", "value": None, "known": False, "sends": None, "standby": None,
                "in_range": None,
                "note": ("traffic_percentage is blank on this rule — unknown, NOT zero. A blank "
                          "must never be read as 'does not send'.")}
    try:
        value = float(text)
    except ValueError:
        return {"raw": str(raw).strip(), "value": None, "known": False, "sends": None,
                "standby": None, "in_range": None,
                "note": f"traffic_percentage {str(raw).strip()!r} is not a number — unknown, not zero."}

    in_range = _MIN <= value <= _MAX
    out = {"raw": str(raw).strip(), "value": value, "known": True, "sends": value > 0,
           "standby": value == 0, "in_range": in_range, "note": ""}
    if not in_range:
        out["note"] = (f"traffic_percentage {value:g} is outside 0–100 — reported as configured, "
                        "not clamped. It is still non-zero, so the channel does send.")
    elif value == 0:
        out["note"] = STANDBY_NOTE
    return out


def verdict(rules, channel=""):
    """One aggregate verdict over a set of rule rows that belong together.

    A group sends if ANY row does; it is `standby` only when EVERY row is provably 0. A group with
    some zeros and some unreadable rows is `None` (unknown), not standby — for the same reason
    blank != 0 above: retiring a channel needs positive evidence, and getting this backwards
    silently deletes live traffic from a blast radius.

    `standby_rules` counts the 0% rows even on a channel that IS sending, because that is the
    dual-vendor case: 100% HTCL + 0% CSL is one channel, carrying traffic, with a second carrier
    provisioned behind it. Reporting only the channel-level verdict would hide the standby entirely.
    """
    entry = {"channel": channel, "rules": 0, "sending": 0, "standby_rules": 0, "unknown": 0}
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        entry["rules"] += 1
        sends = read(rule.get("traffic_percentage")).get("sends")
        entry["sending" if sends is True else
              ("standby_rules" if sends is False else "unknown")] += 1
    # Retained under the old name so existing readers keep working; it counts the same rows.
    entry["idle"] = entry["standby_rules"]
    if entry["sending"]:
        entry["sends"] = True
    elif entry["rules"] and not entry["unknown"]:
        entry["sends"] = False
    else:
        entry["sends"] = None
    # True whenever a 0% row exists at all — the dual-vendor signal, independent of `sends`.
    entry["has_standby"] = entry["standby_rules"] > 0
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


def standby_channels(rules, key=None):
    """Channels whose every rule row is at 0% — provisioned, carrying nothing right now.

    NOT "channels that can be ignored". These are the first thing an outage answer needs.
    """
    return [entry["channel"] for entry in summarise(rules, key) if entry["sends"] is False]


# Old name, same rows. Kept so nothing breaks, but `standby_channels` is the honest one — "idle"
# invites the reading that got this wrong in the first place.
idle_channels = standby_channels
