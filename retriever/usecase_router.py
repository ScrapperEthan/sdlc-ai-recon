"""`tbl_use_case_router` — the AUTHORITATIVE carrier/SLA/delivery-path table, joined on its real
four-column natural key.

## The join

`business_category + channel + route + router`, confirmed by the intranet ingestion 2026-07-29.
This **refutes** RUNBOOK-54 question 1's hypothesis (`channel_rule.route = router.id`); `id` is the
table's own primary key and is not a foreign key from anywhere.

`channel`, `route` and `router` come from a `tbl_use_case_channel_rule` row. `business_category`
lives on `tbl_use_case` — so this is a three-table join, and the master row has to be in hand
before a channel rule can reach its router row. When the rule itself carries a business_category
column, that wins (it is the row-local value); the master is the fallback.

## What it does NOT deliver — read this before quoting a vendor

The table is smaller and emptier than "authoritative" suggests. Measured on the real UAT export:

| | |
| --- | --- |
| rows | **247** |
| child rows whose 4-column key back-links at all | **2967 / 5959 = 49.79%** |
| of those, landing on a row with a non-empty `vendor` | **1628 / 2967 = 54.87%** |
| ⇒ child rows that reach an authoritative vendor | **1628 / 5959 ≈ 27.3%** |
| distinct non-empty `vendor` values | **4**, over 102 of 247 rows (58.70% blank) |

The four values are `AWS HK SNS` (7 rows), `AWS SG SNS` (16), `HTCL` (70), `HTCL OLD` (9) — i.e.
**push and SMS only**. For email, letter, WhatsApp and WeChat the authoritative vendor column is
entirely empty, so the channel-level upper bound in `delivery_chain` is not a stopgap for those
channels; it is the only answer that exists.

## Why the vendor strings are NOT run through the repo-name parser

`retriever/vendors.pick_vendor` takes the RIGHTMOST known token, which is correct for repo names
(`mc-hk-hase-csl-sms-deli-job`) and **wrong here**: `AWS HK SNS` would resolve to `sns` and
`AWS SG SNS` to `sns` too, silently collapsing two regions the authoritative table went to the
trouble of distinguishing — the same class of phantom/merged bucket that RUNBOOK-49/51 fought.
`HTCL OLD` is worse: folding it onto `htcl` would assert that a row explicitly marked OLD routes
through the live gateway.

So these values need their own explicit display-name → token map, owner-confirmed, and this module
ships **no default entries**. Unmapped means `token: None` plus the raw string reported verbatim —
"the authoritative table records `HTCL OLD`" is both more informative and more honest than a guessed
canonical token. The intranet supplies the map under `validation.vendor_display_aliases` in
`config/usecase_columns.json` once the owner confirms; nothing here changes when they do.

Note the tension deliberately left unresolved: `htcl → 3hk` IS already an owner/box-confirmed alias
in this codebase — but for REPO NAMES (RUNBOOK-49). Whether the router table's `HTCL` denotes the
same carrier is a different question, and the intranet explicitly refused to assume it. Correct
call; do not "fix" this by seeding the map.

## delivery_path, by contrast, IS resolved (found 2026-07-30)

RUNBOOK-56 concluded the code table did not exist in anything we could read. It does — three places
in the product's own code agree on it, so unlike the vendor names this is not a judgement call and
the map ships as a built-in default. See `DEFAULT_DELIVERY_PATH_LABELS`.

The two things to keep straight:

* It is a **classification**, not a routing control: which timeliness tier under which delivery
  system. A message is *classified on* path 3; it does not *travel via* path 3.
* **`tbl_template.delivery_path` is a different column with a different meaning** (concrete channels
  — SFMC / ICCM / 3HK / AWS SNS). An earlier version of this module treated the two as one thing,
  which is why the note now says so explicitly.
"""
import json
import os
import re

from . import config
from .usecase_catalog import (
    _ROUTER_FIELDS,
    _column_map,
    _citation_for,
    _field,
    _load_csv,
    _table_path,
    active_dataset,
)

# Mirrored from the intranet's config/usecase_columns.json (`validation.router_natural_key`).
# That file is authoritative when present; this default exists so local tests and a box that has
# not written the knob yet behave identically. It is NOT a guess — the intranet reported it.
DEFAULT_NATURAL_KEY = ("business_category", "channel", "route", "router")

# Deliberately empty — see the module docstring. A guessed entry here would fabricate a carrier.
DEFAULT_VENDOR_DISPLAY_ALIASES = {}

# RUNBOOK-54 question 6, answered by the intranet 2026-07-30 from evidence rather than by asking:
# the product code compares `process_millisecond <= message_process_sla` and
# `delivery_millisecond <= message_delivery_sla`, and every value in the export lands on a round
# number of minutes when read as ms (60000 = 1 min … 86400000 = 24 h). Both columns are identical on
# all 247 rows. That is strong enough to render a duration, which is far more useful than a bare
# integer — but no owner has signed it off, so the claim travels with its basis attached.
SLA_UNIT = "milliseconds"
_SLA_NOTE = (
    "SLA columns are milliseconds. Evidence: the product code compares `process_millisecond <= "
    "message_process_sla`, and all eight distinct values are exact minutes when read as ms "
    "(60000 = 1 min, 300000 = 5 min, 86400000 = 24 h). Not owner-signed-off, so say 'the "
    "configured SLA is 60000 ms (1 minute)' — give the raw number alongside the duration, and note "
    "that the unit is inferred from code, not from a business sign-off. `message_process_sla` and "
    "`message_delivery_sla` are identical on all 247 rows, so do not present them as two "
    "independent budgets.")

# ---------------------------------------------------------------------------------------------
# delivery_path — the code table, FOUND 2026-07-30 after RUNBOOK-56 concluded it did not exist in
# anything we could read. It does: `powermi/constant/DeliveryPathEnum.java` in portal-web, with
# `BillingReportService.java` joining route/router + business_category and converting the code to
# these names, and the same map again in the portal frontend (`static/dist/js/util/data.js:174`).
#
# Shipped as a built-in default rather than left to config, because unlike the vendor display names
# this is not a judgement call: three independent places in the product's own code agree on it.
# Config can still override, for when the enum changes on their side.
#
# ⚠️ It is NOT the same column as `tbl_template.delivery_path`, which carries concrete delivery
# CHANNELS (SFMC / ICCM / 3HK / AWS SNS). Same name, different meaning — an earlier note in this
# module treated them as one thing and was wrong.
DEFAULT_DELIVERY_PATH_LABELS = {
    "1": "Time Critical (MDC)",
    "2": "HASE Real Time Express (MDC)",
    "3": "HASE Real Time Standard (MDC)",
    "4": "HASE Batch (MDC)",
    "5": "Time Critical",
    "6": "HASE Real Time Express",
    "7": "HASE Real Time Standard",
    "8": "Shared Real Time Standard",
    "9": "Shared Batch",
}
# Defined in the enum but with no rows in the UAT export. "Defined but unused here" is a different
# statement from "undefined", and only the first is true of 7.
DELIVERY_PATH_DEFINED_UNUSED = ("7",)

_DELIVERY_PATH_NOTE = (
    "delivery_path is Power MI's message delivery-path CLASSIFICATION code: which timeliness tier "
    "(Time Critical / Real Time Express / Real Time Standard / Batch) under which delivery system "
    "(MDC / HASE / Shared) this route belongs to. It is NOT a vendor or channel number, and there is "
    "no evidence it controls the runtime delivery topic or job — do not say a message travels 'via "
    "path 3'; say it is classified on that path. It also cannot be derived from business_category: "
    "one category carries several delivery_paths. "
    "⚠️ `tbl_template.delivery_path` is a DIFFERENT column with a different meaning (concrete "
    "channels: SFMC / ICCM / 3HK / AWS SNS) — never read one as the other.")

_UNCONFIRMED_ALIAS_NOTE = (
    "vendor recorded verbatim in tbl_use_case_router; the display-name -> canonical-token mapping "
    "is NOT owner-confirmed, so it is reported as raw text. Do NOT translate it yourself (in "
    "particular do not read `HTCL` as 3hk or `AWS HK SNS` as sns — the repo-name parser's "
    "rightmost-token rule collapses the AWS regions and would treat an OLD gateway as live)."
)


def _columns_config():
    try:
        with open(config.USECASE_COLUMNS_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def natural_key_fields():
    """The join key, from the intranet knob when present, else the reported default."""
    validation = (_columns_config().get("validation") or {})
    declared = validation.get("router_natural_key")
    fields = tuple(str(name).strip() for name in declared or () if str(name).strip())
    return fields or DEFAULT_NATURAL_KEY


def vendor_display_aliases():
    """{raw display name (casefolded) -> canonical token}. Empty until an owner confirms."""
    validation = (_columns_config().get("validation") or {})
    declared = validation.get("vendor_display_aliases")
    if not isinstance(declared, dict):
        return dict(DEFAULT_VENDOR_DISPLAY_ALIASES)
    return {str(key).strip().casefold(): str(value).strip().lower()
            for key, value in declared.items() if not str(key).startswith("_")}


def _router_rows():
    dataset = active_dataset()
    path = _table_path(dataset, "tbl_use_case_router")
    rows, cols = _load_csv(path)
    if not rows:
        return [], {}, path
    bound, _ambiguous = _column_map(cols, _ROUTER_FIELDS)
    return rows, bound, path


def _key_of(values, fields):
    """The lookup key, or None when ANY component is blank.

    A partial key must never match: with `business_category + channel + route + router`, dropping a
    blank component would let one use case's rule land on an unrelated carrier's row. An incomplete
    key is a reported miss, which is why roughly half the child rows legitimately do not back-link.
    """
    key = []
    for field in fields:
        value = (values.get(field) or "").strip()
        if not value:
            return None
        key.append(value.casefold())
    return tuple(key)


def index_by_natural_key():
    """{key_tuple: [router row, …]} plus provenance. Lists, not single rows: the intranet reported
    the key as unique, and a duplicate would be a data-contract change we must surface rather than
    silently take the first of."""
    fields = natural_key_fields()
    rows, bound, path = _router_rows()
    # "table absent" and "table present but the key columns didn't bind" are different failures and
    # get different remedies (export the table vs fix the column map), so never collapse them.
    unbound = [field for field in fields if field not in bound] if rows else []
    index = {}
    incomplete = 0
    for line_no, row in rows:
        values = {field: _field(row, bound, field) for field in _ROUTER_FIELDS if field in bound}
        key = _key_of(values, fields) if not unbound else None
        if key is None:
            incomplete += 1
            continue
        index.setdefault(key, []).append({
            **values,
            "citation": _citation_for(path, line_no),
        })
    return {
        "available": bool(rows) and not unbound,
        "table_present": bool(rows),
        "key_fields": list(fields),
        "unbound_key_fields": unbound,
        "index": index,
        "row_count": len(rows),
        "rows_with_incomplete_key": incomplete,
        "path": path,
    }


# Read off the display string itself, not from any mapping. `AWS HK SNS` literally contains a
# provider family and a region, and reporting those two is a different (weaker, checkable) claim
# than asserting which canonical carrier token the row denotes. Two layers because the intranet's
# 2026-07-30 analysis is right about why it matters: a REGIONAL outage must be counted per region
# (the Java side defines AWS_HK_SNS_PUSH and AWS_SG_SNS_PUSH separately, with 4 and 5 delivery jobs
# behind them), while an AWS-SNS-wide outage merges them.
_FAMILIES = ("sns", "smsc", "apns", "fcm")
_REGIONS = {"hk": "hk", "sg": "sg", "hongkong": "hk", "singapore": "sg"}
# A carrier explicitly marked OLD must never be reported as the live one. Folding `HTCL OLD` onto
# `HTCL` would assert that a legacy route is carrying traffic; the intranet found the opposite is
# unprovable either way (9 router rows, 2 live channel rules with status=Y, the enum
# MessageRouterTopicEnum.HTCL_OLD_SMS still in the code, but no dedicated delivery-job repo).
_LEGACY_MARKERS = ("old", "legacy", "deprecated")


def _describe_display_name(text):
    """Decompose what the string SAYS: family, region, lifecycle. Asserts no carrier identity."""
    words = [word for word in re.split(r"[^A-Za-z0-9]+", text.lower()) if word]
    family = next((word for word in words if word in _FAMILIES), "")
    region = next((_REGIONS[word] for word in words if word in _REGIONS), "")
    lifecycle = "legacy" if any(word in _LEGACY_MARKERS for word in words) else "active"
    return {"provider_family": family, "region": region, "lifecycle": lifecycle}


def delivery_path_labels():
    """{code -> label}. Intranet knob wins when present, else the code-evidenced default."""
    validation = (_columns_config().get("validation") or {})
    declared = validation.get("delivery_path_labels")
    if not isinstance(declared, dict):
        return dict(DEFAULT_DELIVERY_PATH_LABELS)
    labels = {str(key).strip(): str(value).strip()
              for key, value in declared.items() if not str(key).startswith("_")}
    return labels or dict(DEFAULT_DELIVERY_PATH_LABELS)


# Read off the authoritative label, the same weak-but-checkable move used for vendor display names:
# the label itself says which delivery system and which timeliness tier, so reporting those two is
# not a new claim. `system` is the (MDC) / bare / Shared distinction the enum draws; `tier` is the
# Time Critical / Real Time Express / Real Time Standard / Batch ladder.
_PATH_TIERS = ("Time Critical", "Real Time Express", "Real Time Standard", "Batch")


def resolve_delivery_path(code):
    """Raw `delivery_path` cell -> {code, label, system, tier, known, note}.

    Never invents a label: an unknown code is reported as a bare number, because the nine textual
    'Path' strings that appear in alert titles were only ever candidate guesses and are NOT this
    enum's names.
    """
    text = str(code or "").strip()
    if not text:
        return {"code": "", "label": "", "system": "", "tier": "", "known": False,
                "note": "delivery_path is blank on the matched router row."}
    label = delivery_path_labels().get(text, "")
    if not label:
        return {"code": text, "label": "", "system": "", "tier": "", "known": False,
                "note": (f"delivery_path {text!r} is not in the code table (known codes: "
                          + ", ".join(sorted(delivery_path_labels())) +
                          "). Report the number and say the name is unknown — do NOT reuse a "
                          "textual 'Path' string from an alert title, those are a different thing. "
                          + _DELIVERY_PATH_NOTE)}
    if "(MDC)" in label:
        system = "MDC"
    elif label.startswith("Shared"):
        system = "Shared"
    else:
        system = "HASE"
    tier = next((name for name in _PATH_TIERS if name in label), "")
    out = {"code": text, "label": label, "system": system, "tier": tier, "known": True,
           "note": _DELIVERY_PATH_NOTE}
    if text in DELIVERY_PATH_DEFINED_UNUSED:
        out["note"] = ("this code is DEFINED in DeliveryPathEnum but has no rows in the UAT export — "
                       '"defined but unused here" is not the same as "undefined". ' + out["note"])
    return out


def resolve_vendor(raw):
    """Raw `vendor` cell -> {raw, token, confirmed, family, region, lifecycle, note}.

    Never guesses a token. Does decompose the display name, because "family sns, region hk, marked
    legacy" is information the raw string genuinely carries, and withholding it while an incident is
    running would be its own kind of dishonesty.
    """
    text = (raw or "").strip()
    if not text:
        return {"raw": "", "token": None, "confirmed": False, "present": False,
                "provider_family": "", "region": "", "lifecycle": "",
                "note": ("tbl_use_case_router.vendor is blank on the matched row (58.70% of the "
                          "table is). The authoritative carrier is simply not recorded — that is "
                          "not the same as 'no carrier'.")}
    described = _describe_display_name(text)
    token = vendor_display_aliases().get(text.casefold())
    out = {"raw": text, "token": token, "confirmed": bool(token), "present": True, **described}
    if token and described["lifecycle"] == "legacy":
        # Even an owner-confirmed alias does not license dropping the legacy marker: the token says
        # WHO, the lifecycle says WHETHER IT IS CURRENT, and only the first was ever confirmed.
        out["note"] = ("mapped to a canonical carrier, but this row is marked LEGACY — report both. "
                       "A confirmed alias answers 'which carrier', not 'is this route live'.")
    elif token:
        out["note"] = ""
    else:
        out["note"] = _UNCONFIRMED_ALIAS_NOTE
        if described["lifecycle"] == "legacy":
            out["note"] += (" This value is also marked OLD/legacy: it must never be folded onto the "
                            "same carrier as the un-marked value, which would assert that a legacy "
                            "route is live.")
    return out


def router_for_rule(rule, business_category="", index=None):
    """One channel-rule row -> its authoritative router row, or a stated reason why not.

    Fail-closed and left-join by construction: every non-match returns `matched: False` with a
    `reason`, and no branch here ever falls back to a looser key.
    """
    index = index if index is not None else index_by_natural_key()
    result = {"matched": False, "reason": "", "key_fields": index["key_fields"]}
    if not index["available"]:
        result["reason"] = (
            "tbl_use_case_router is present but its key columns did not bind ("
            + ", ".join(index["unbound_key_fields"]) + ") — fix the intranet column map, "
            "config/usecase_columns.json"
            if index.get("unbound_key_fields")
            else "tbl_use_case_router is not in the active dataset")
        return result

    rule = rule or {}
    # A business_category on the rule row itself is row-local and wins; the master row is fallback.
    # Which one supplied it is reported, because it changes what a miss means: with `rule`, the join
    # is two tables and a use case with no master row can still reach its carrier; with `master`, no
    # master row means no authoritative carrier however complete the channel rules are.
    rule_category = (rule.get("business_category") or "").strip()
    master_category = (business_category or "").strip()
    values = {
        "business_category": rule_category or master_category,
        "channel": rule.get("channel") or "",
        "route": rule.get("route") or "",
        "router": rule.get("router") or "",
    }
    result["business_category_source"] = (
        "rule" if rule_category else ("master" if master_category else "absent"))
    if rule_category and master_category and rule_category.casefold() != master_category.casefold():
        # Not resolved silently: the two tables disagreeing means one of them keys onto a different
        # carrier's router row, and picking the wrong one is precisely the failure RUNBOOK-54 was
        # written to prevent. Use the row-local value and say so loudly.
        result["business_category_conflict"] = (
            f"tbl_use_case_channel_rule says {rule_category!r} but tbl_use_case says "
            f"{master_category!r}. Using the rule row (row-local), but the two disagree, so this "
            "carrier answer is suspect — the tables are also from different export runs. Report the "
            "disagreement rather than the vendor alone.")
    key = _key_of(values, index["key_fields"])
    if key is None:
        missing = [f for f in index["key_fields"] if not (values.get(f) or "").strip()]
        result["reason"] = ("incomplete natural key — blank " + ", ".join(missing)
                            + "; a partial key is never matched (it would land on another "
                              "carrier's row). This is why ~50% of rows do not back-link.")
        result["missing_key_fields"] = missing
        return result
    matches = index["index"].get(key) or []
    if not matches:
        result["reason"] = ("no tbl_use_case_router row for this key — the four tables also come "
                            "from different export runs (mixed_export_times), so this can mean "
                            "'exported at a different moment', not 'not configured'.")
        return result
    if len(matches) > 1:
        result["reason"] = (f"{len(matches)} router rows share this natural key, which the intranet "
                            "reported as unique — refusing to pick one. Data-contract change.")
        result["ambiguous_citations"] = [row["citation"] for row in matches]
        return result

    row = matches[0]
    result.update({
        "matched": True,
        "router_id": row.get("id") or "",
        "vendor": resolve_vendor(row.get("vendor")),
        "delivery_path": row.get("delivery_path") or "",
        "message_process_sla": row.get("message_process_sla") or "",
        "message_delivery_sla": row.get("message_delivery_sla") or "",
        "business_category": row.get("business_category") or "",
        "citation": row.get("citation") or "",
    })
    if result["message_process_sla"] or result["message_delivery_sla"]:
        result["sla_note"] = _SLA_NOTE
        result["sla_unit"] = SLA_UNIT
    if result["delivery_path"]:
        # The raw code stays on `delivery_path` (nothing that read it before changes); the resolved
        # name, delivery system and timeliness tier arrive alongside it.
        resolved = resolve_delivery_path(result["delivery_path"])
        result["delivery_path_resolved"] = resolved
        result["delivery_path_label"] = resolved["label"]
        result["delivery_path_note"] = resolved["note"]
    return result


def coverage_note():
    """The one-paragraph honesty envelope for any answer that quotes this table."""
    index = index_by_natural_key()
    if not index["available"]:
        return ("tbl_use_case_router is not usable in the active dataset; carrier answers stay at "
                "the channel-level upper bound.")
    return (
        f"tbl_use_case_router: {index['row_count']} rows, joined on "
        f"{' + '.join(index['key_fields'])}. Measured on the real UAT export, only ~49.8% of child "
        "rows back-link and 58.7% of router rows have a blank vendor, so roughly a QUARTER of rows "
        "reach an authoritative carrier — and the four values present cover push and SMS only. "
        "Absence here is never evidence of no carrier.")
