"""Read-only ingest + join for the Use Case master data across all three UAT tables
(tbl_use_case, tbl_use_case_channel_rule, tbl_use_case_ext), manifest-driven and
environment-aware.

Round A fixes the P0s found when the just-shipped Tier 0 (dev/SCT, single-table) met real UAT
data: it labelled UAT rows "dev/SCT", bound `status` to `unknown_bounce_back_status` (first-needle-
wins), and computed routed/coverage off the OLD dev/SCT route snapshot. This module reads a
manifest-declared dataset directory (``config.USECASE_DATASET_DIR``) so provenance is real and a
route join can never silently cross environments. ``retriever/usecase_master.py`` is now a thin
facade over this module -- kept so `impact_report.py`, `outage_report.py`, `webapp/tools.py`,
`mcp_server.py`, and `retrieval_service.py` need no import churn.

Round A channels come from the `tbl_use_case_channel_rule.channel` column (FACT), not a parsed
decision tree -- `rule_text` is stored raw only; the AST is Round B. Missing file at any layer ->
available:False / empty results, never a crash.
"""
import csv
import json
import os
import re
from datetime import datetime, timezone

from . import config

# Seed dict for business_category — source of truth is BusinessCategoryEnum.java on the mirror.
# Any CSV code missing from this dict (e.g. 33, 37) is a data-contract drift alarm, not a bug here.
BUSINESS_CATEGORY_ENUM = {
    0: "WPB_REALTIME_MARKETING", 1: "WPB_REALTIME_SERVICING", 2: "WPB_BATCH_SERVICING",
    3: "WPB_BATCH_MARKETING", 4: "WPB_HR_REALTIME_SERVICING", 5: "WPB_SEC_REALTIME_SERVICING",
    6: "CMB", 7: "WPB_HS_REALTIME_SERVICING", 8: "WPB_TC_REALTIME_SERVICING",
    10: "HASE_WPB_SERVICING_REALTIME_GENERAL", 11: "HASE_WPB_SERVICING_REALTIME_HIGHRISK",
    12: "HASE_WPB_SERVICING_BATCH", 13: "HASE_WPB_MARKETING_REALTIME_GENERAL",
    14: "HASE_WPB_MARKETING_BATCH", 15: "HASE_CMB_SERVICING_REALTIME_GENERAL",
    16: "HASE_CMB_SERVICING_REALTIME_HIGHRISK", 17: "HASE_CMB_SERVICING_BATCH",
    18: "HASE_CMB_MARKETING_REALTIME_GENERAL", 19: "HASE_CMB_MARKETING_BATCH",
    20: "HASE_WPB_SERVICING_TIMECRITICAL", 21: "HASE_CMB_SERVICING_TIMECRITICAL",
    32: "HSBC_WPB_SERVICING_BATCH",
    34: "HSBC_WPB_SERVICING_TIMECRITICAL", 35: "HSBC_WPB_SERVICING_REALTIME_HIGHRISK",
}

_JUNK_WORK_STREAM = {"invalid", "test", "n/a", "na", "null", "none", "-", "tbd", "xxx", "unknown", ""}
_STALE_DAYS = 365
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                 "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y")

# ---------------------------------------------------------------------------
# Column binding: exact -> alias -> unique-fuzzy -> ambiguity (Building block 2)
# ---------------------------------------------------------------------------

# {field: {"exact": {flattened accepted names, incl. aliases}, "needles": fallback fuzzy tuple}}
# "exact" folds in the old alias-map step too -- both are exact-equality checks against a set, so
# whether a name got there as the canonical spelling or an accepted variant doesn't change the
# matching rule (>1 distinct column among them is still ambiguous, never silently picked).
_FIELD_SPECS = {
    "use_case_id": {"exact": {"usecaseid"}, "needles": ("usecase", "id")},
    "use_case_name": {"exact": {"usecasename"}, "needles": ("usecase", "name")},
    "project_name": {"exact": {"projectname"}, "needles": ("project",)},
    "source_system": {"exact": {"sourcesystem"}, "needles": ("source", "system")},
    "work_stream_name": {"exact": {"workstreamname"}, "needles": ("workstream",)},
    "line_of_business": {"exact": {"lineofbusiness"}, "needles": ("lineofbusiness",)},
    "business_category": {"exact": {"businesscategory"}, "needles": ("business", "category")},
    "country_code": {"exact": {"countrycode"}, "needles": ("country",)},
    "group_member": {"exact": {"groupmember"}, "needles": ("group", "member")},
    "app_name": {"exact": {"appname"}, "needles": ("app", "name")},
    "created_by": {"exact": {"createdby"}, "needles": ("created", "by")},
    "created_time": {"exact": {"createdtime"}, "needles": ("created", "time")},
    "modified_by": {"exact": {"modifiedby"}, "needles": ("modified", "by")},
    "last_modified_time": {"exact": {"lastmodifiedtime"}, "needles": ("last", "modified", "time")},
    # exact-only: "status" must win over "unknown_bounce_back_status" (defect #3). A fuzzy
    # ("status",) fallback would match either — only offered when the literal column is absent.
    "status": {"exact": {"status"}, "needles": ("status",)},

    # Consent / opt-in flags — matched on the FULL flattened name; several are near-duplicates
    # (push_optin_flag vs marketing_push_optin_flag vs high_risk_push_optin_flag …) so no fuzzy
    # fallback is offered here — an unmatched consent column simply stays unbound.
    "marketing_optin_flag": {"exact": {"marketingoptinflag"}},
    "push_optin_flag": {"exact": {"pushoptinflag"}},
    "marketing_push_optin_flag": {"exact": {"marketingpushoptinflag"}},
    "high_risk_push_optin_flag": {"exact": {"highriskpushoptinflag"}},
    "securities_push_optin_flag": {"exact": {"securitiespushoptinflag"}},
    # defect #4: UAT uses the SINGULAR "insight"; accept both.
    "marketing_insight_push_optin_flag": {
        "exact": {"marketinginsightpushoptinflag", "marketinginsightspushoptinflag"},
    },
    "sms_optin_flag": {"exact": {"smsoptinflag"}},
    "marketing_sms_optin_flag": {"exact": {"marketingsmsoptinflag"}},
    "email_optin_flag": {"exact": {"emailoptinflag"}},
    "marketing_email_optin_flag": {"exact": {"marketingemailoptinflag"}},
    "mms_optin_flag": {"exact": {"mmsoptinflag"}},
    "marketing_mms_optin_flag": {"exact": {"marketingmmsoptinflag"}},
    "wechat_optin_flag": {"exact": {"wechatoptinflag"}},
    "marketing_wechat_optin_flag": {"exact": {"marketingwechatoptinflag"}},
    "whatsapp_optin_flag": {"exact": {"whatsappoptinflag"}},
    "marketing_whatsapp_optin_flag": {"exact": {"marketingwhatsappoptinflag"}},

    # tbl_use_case_channel_rule
    "channel": {"exact": {"channel"}, "needles": ("channel",)},
    "priority": {"exact": {"priority"}, "needles": ("priority",)},
    "route": {"exact": {"route"}, "needles": ("route",)},
    "router": {"exact": {"router"}, "needles": ("router",)},
    "traffic_percentage": {"exact": {"trafficpercentage"}, "needles": ("traffic",)},
    "tag": {"exact": {"tag"}, "needles": ("tag",)},
    "sender": {"exact": {"sender"}, "needles": ("sender",)},
    "send_policy": {"exact": {"sendpolicy"}, "needles": ("send", "policy")},

    # tbl_use_case_ext
    "service_line": {"exact": {"serviceline"}, "needles": ("service", "line")},
    "messaging_service_level": {"exact": {"messagingservicelevel"}, "needles": ("service", "level")},
    "delivery_mode": {"exact": {"deliverymode"}, "needles": ("delivery", "mode")},
    "endpoint": {"exact": {"endpoint"}, "needles": ("endpoint",)},
    "rule_text": {"exact": {"ruletext"}, "needles": ("rule", "text")},
    "message_owner": {"exact": {"messageowner"}, "needles": ("message", "owner")},
    "business_contact": {"exact": {"businesscontact"}, "needles": ("business", "contact")},
    "business_team": {"exact": {"businessteam"}, "needles": ("business", "team")},
    "team_head": {"exact": {"teamhead"}, "needles": ("team", "head")},
    "depart_head": {"exact": {"departhead"}, "needles": ("depart", "head")},
    "cost_owner": {"exact": {"costowner"}, "needles": ("cost", "owner")},
    "signoff_by": {"exact": {"signoffby"}, "needles": ("signoff",)},
    "downstream_name": {"exact": {"downstreamname"}, "needles": ("downstream", "name")},
    "is_dual_channel": {"exact": {"isdualchannel"}, "needles": ("dual", "channel")},
    "support_dual_vendor": {"exact": {"supportdualvendor"}, "needles": ("dual", "vendor")},
    "regulatory_requirement": {"exact": {"regulatoryrequirement"}, "needles": ("regulatory",)},
    "high_risk_flag": {"exact": {"highriskflag"}, "needles": ("high", "risk", "flag")},
    "dormant_period": {"exact": {"dormantperiod"}, "needles": ("dormant",)},  # Word-only; may be absent
}

_CONSENT_FIELD_LABELS = {
    "marketing_optin_flag": "Marketing Consent",
    "push_optin_flag": "Push",
    "marketing_push_optin_flag": "Marketing Push",
    "high_risk_push_optin_flag": "High-Risk Push",
    "securities_push_optin_flag": "Securities Push",
    "marketing_insight_push_optin_flag": "Marketing Insight Push",
    "sms_optin_flag": "SMS",
    "marketing_sms_optin_flag": "Marketing SMS",
    "email_optin_flag": "Email",
    "marketing_email_optin_flag": "Marketing Email",
    "mms_optin_flag": "MMS",
    "marketing_mms_optin_flag": "Marketing MMS",
    "wechat_optin_flag": "WeChat",
    "marketing_wechat_optin_flag": "Marketing WeChat",
    "whatsapp_optin_flag": "WhatsApp",
    "marketing_whatsapp_optin_flag": "Marketing WhatsApp",
}

_IDENTITY_FIELDS = (
    "use_case_id", "use_case_name", "project_name", "source_system", "work_stream_name",
    "line_of_business", "business_category", "country_code", "group_member", "app_name",
    "created_by", "created_time", "modified_by", "last_modified_time", "status",
)
_CONSENT_FIELDS = tuple(_CONSENT_FIELD_LABELS)
_RULE_FIELDS = ("use_case_id", "channel", "priority", "route", "router", "traffic_percentage",
                "tag", "sender", "send_policy", "status")
_EXT_FIELDS = ("use_case_id", "service_line", "messaging_service_level", "delivery_mode", "endpoint",
               "rule_text", "message_owner", "business_contact", "business_team", "team_head",
               "depart_head", "cost_owner", "signoff_by", "downstream_name", "is_dual_channel",
               "support_dual_vendor", "regulatory_requirement", "high_risk_flag", "dormant_period")


def _flat(col):
    return re.sub(r"[^a-z0-9]", "", (col or "").lower())


def resolve_column(cols, field):
    """(column_or_None, ambiguous_candidates). exact/alias names win outright; a fuzzy needle
    fallback only applies when exactly one column matches it; >1 candidate at either stage binds
    None and reports every candidate — never a silent first-match pick."""
    spec = _FIELD_SPECS.get(field) or {}
    flat_cols = [(c, _flat(c)) for c in cols]
    exact_names = spec.get("exact") or set()
    exact_matches = [c for c, f in flat_cols if f in exact_names]
    if len(exact_matches) == 1:
        return exact_matches[0], []
    if len(exact_matches) > 1:
        return None, exact_matches
    needles = spec.get("needles")
    if needles:
        fuzzy_matches = [c for c, f in flat_cols if all(n in f for n in needles)]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], []
        if len(fuzzy_matches) > 1:
            return None, fuzzy_matches
    return None, []


def _column_map(cols, fields):
    bound, ambiguous = {}, {}
    for field in fields:
        col, candidates = resolve_column(cols, field)
        if col:
            bound[field] = col
        elif candidates:
            ambiguous[field] = candidates
    return bound, ambiguous


def _master_column_map(cols):
    return _column_map(cols, _IDENTITY_FIELDS + _CONSENT_FIELDS)


def _field(row, bound, name):
    col = bound.get(name)
    return (row.get(col) or "").strip() if col else ""


# ---------------------------------------------------------------------------
# Dataset / manifest (Building block 1)
# ---------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active_dataset():
    """The manifest-driven dataset at ``config.USECASE_DATASET_DIR``, or a synthesized legacy
    one-table dataset from the pre-Round-A ``config.USECASE_MASTER_CSV`` (back-compat), or None
    when nothing is configured at all."""
    manifest_path = os.path.join(config.USECASE_DATASET_DIR, "manifest.json")
    manifest = _read_json(manifest_path)
    if manifest is not None:
        tables = {}
        for name, meta in (manifest.get("tables") or {}).items():
            if not isinstance(meta, dict) or not meta.get("file"):
                continue
            tables[name] = {
                "path": os.path.join(config.USECASE_DATASET_DIR, meta["file"]),
                "row_count": meta.get("row_count"),
            }
        return {
            "environment": manifest.get("environment") or "unknown",
            "snapshot_id": manifest.get("snapshot_id"),
            "exported_at": manifest.get("exported_at"),
            "tables": tables,
            "legacy": False,
        }
    if os.path.exists(config.USECASE_MASTER_CSV):
        return {
            "environment": os.environ.get("SDLC_USECASE_ENV", "unknown"),
            "snapshot_id": None,
            "exported_at": None,
            "tables": {"tbl_use_case": {"path": config.USECASE_MASTER_CSV, "row_count": None}},
            "legacy": True,
        }
    return None


def _table_path(dataset, name):
    if not dataset:
        return None
    meta = (dataset.get("tables") or {}).get(name)
    return meta["path"] if meta else None


def _citation_for(path, line_no):
    try:
        rel = os.path.relpath(path, config.ROOT)
    except ValueError:
        rel = path
    return rel.replace(os.sep, "/") + ":" + str(line_no)


_CSV_CACHE = {}


def _load_csv(path):
    """([(line_no, row), …], cols) for one CSV, cached on (path, mtime, size) — Building block 8.
    Missing/blank path -> ([], []), never crash."""
    if not path:
        return [], []
    try:
        stat = os.stat(path)
    except OSError:
        _CSV_CACHE.pop(path, None)
        return [], []
    sig = (stat.st_mtime, stat.st_size)
    cached = _CSV_CACHE.get(path)
    if cached and cached[0] == sig:
        return cached[1], cached[2]
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            cols = reader.fieldnames or []
            rows = [(line_no, row) for line_no, row in enumerate(reader, 2)]
    except OSError:
        rows, cols = [], []
    _CSV_CACHE[path] = (sig, rows, cols)
    return rows, cols


def _master_rows():
    dataset = active_dataset()
    path = _table_path(dataset, "tbl_use_case")
    rows, cols = _load_csv(path)
    return dataset, path, rows, cols


def _column_bindings(dataset):
    path = _table_path(dataset, "tbl_use_case")
    _rows, cols = _load_csv(path)
    if not cols:
        return {"bound": {}, "ambiguous": {}}
    bound, ambiguous = _master_column_map(cols)
    return {"bound": bound, "ambiguous": ambiguous}


def snapshot_manifest():
    """Provenance envelope — real `environment`/`snapshot_id`/`exported_at`/`row_count` from the
    manifest (kills defect #1: UAT no longer labelled "dev/SCT"). Legacy back-compat mode reports
    `environment` from SDLC_USECASE_ENV (default "unknown"), never a hardcoded label."""
    dataset = active_dataset()
    bindings = _column_bindings(dataset)
    path = _table_path(dataset, "tbl_use_case")
    if not dataset or not path:
        return {
            "environment": "unknown", "source_table": "tbl_use_case", "snapshot_id": None,
            "exported_at": None, "row_count": 0, "production_verified": False,
            "caveat": "no Use Case dataset configured (index/usecase-snapshots/active/manifest.json absent).",
            "column_bindings": bindings,
        }
    manifest = {
        "environment": dataset["environment"],
        "source_table": "tbl_use_case",
        "snapshot_id": dataset.get("snapshot_id"),
        "exported_at": dataset.get("exported_at"),
        "row_count": 0,
        "production_verified": False,
        "caveat": (f"{dataset['environment']} snapshot — indicative, NOT production. Absence here "
                   "does not prove absence in production."),
        "column_bindings": bindings,
    }
    if dataset.get("legacy"):
        try:
            mtime = os.path.getmtime(path)
            manifest["exported_at"] = (
                datetime.fromtimestamp(mtime, timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z")
            )
        except OSError:
            pass
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            manifest["row_count"] = max(0, sum(1 for _ in handle) - 1)
    except OSError:
        pass
    return manifest


def _detect_usecase_col(cols):
    for c in cols:
        f = _flat(c)
        if "usecase" in f and "id" in f:
            return c
    for c in cols:
        if "usecase" in _flat(c):
            return c
    return None


def _route_table_path(dataset):
    """Same-environment route snapshot path, or None. Legacy datasets (no manifest) fall back to
    the pre-Round-A single-file `config.USECASE_SNAPSHOT_CSV` exactly like the old code did — that
    is not a cross-environment join, there is only ever one file in play. A manifest-driven dataset
    must declare its OWN `tbl_event_router_usecase_topic` table; if it doesn't, the route dimension
    is unavailable and this must NEVER reach across to a differently-labelled snapshot (defect #2:
    UAT coverage computed off the stale dev/SCT route file)."""
    if not dataset:
        return None
    if dataset.get("legacy"):
        return config.USECASE_SNAPSHOT_CSV if os.path.exists(config.USECASE_SNAPSHOT_CSV) else None
    return _table_path(dataset, "tbl_event_router_usecase_topic")


def route_dimension():
    """{"available": bool, "reason": str|None} — the cross-environment join guard."""
    dataset = active_dataset()
    path = _route_table_path(dataset)
    if not path or not os.path.exists(path):
        return {"available": False, "reason": "no same-environment route snapshot"}
    return {"available": True, "reason": None}


def _routed_use_case_ids():
    dataset = active_dataset()
    path = _route_table_path(dataset)
    if not path:
        return set()
    rows, cols = _load_csv(path)
    if not rows:
        return set()
    uc_col = _detect_usecase_col(cols)
    if not uc_col:
        return set()
    return {(row.get(uc_col) or "").strip().lower() for _ln, row in rows if (row.get(uc_col) or "").strip()}


def is_stale(last_modified_time, created_time=""):
    """Public wrapper: True/False whether last_modified_time (falling back to created_time) is
    older than 12 months. None if neither date is parseable — unknown, not asserted either way."""
    return _is_stale(last_modified_time, created_time)


def _parse_dt(text):
    text = (text or "").strip()
    if not text:
        return None
    for candidate in (text, text[:19], text[:10]):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def _is_stale(last_modified_time, created_time, reference=None):
    reference = reference or datetime.now(timezone.utc).replace(tzinfo=None)
    dt = _parse_dt(last_modified_time) or _parse_dt(created_time)
    if dt is None:
        return None
    return (reference - dt).days > _STALE_DAYS


def _category_label(code_raw):
    if not code_raw:
        return "UNKNOWN()"
    try:
        code = int(float(code_raw))
    except ValueError:
        return f"UNKNOWN({code_raw})"
    return BUSINESS_CATEGORY_ENUM.get(code) or f"UNKNOWN({code})"


def _identity(path, line_no, row, bound):
    code_raw = _field(row, bound, "business_category")
    status_raw = _field(row, bound, "status")
    return {
        "use_case_id": _field(row, bound, "use_case_id"),
        "name": _field(row, bound, "use_case_name"),
        "project": _field(row, bound, "project_name"),
        "source_system": _field(row, bound, "source_system"),
        "work_stream": _field(row, bound, "work_stream_name"),
        "line_of_business": _field(row, bound, "line_of_business"),
        "business_category_code": code_raw,
        "business_category_label": _category_label(code_raw),
        "country": _field(row, bound, "country_code"),
        "group_member": _field(row, bound, "group_member"),
        "app": _field(row, bound, "app_name"),
        "created_by": _field(row, bound, "created_by"),
        "created_time": _field(row, bound, "created_time"),
        "modified_by": _field(row, bound, "modified_by"),
        "last_modified_time": _field(row, bound, "last_modified_time"),
        "status": status_raw,
        "active": (status_raw.upper() == "Y") if status_raw else None,
        "citation": _citation_for(path, line_no),
    }


def master_for(use_case_id):
    """The joined Use Case identity for one id, or None if absent from the master snapshot
    (missing entirely, or the dataset itself is absent)."""
    needle = (use_case_id or "").strip().lower()
    if not needle:
        return None
    _dataset, path, rows, cols = _master_rows()
    if not rows:
        return None
    bound, _ambiguous = _master_column_map(cols)
    id_col = bound.get("use_case_id")
    if not id_col:
        return None
    for line_no, row in rows:
        if (row.get(id_col) or "").strip().lower() == needle:
            return _identity(path, line_no, row, bound)
    return None


def consent_preflight(use_case_id):
    """Pre-send consent/opt-in switches that are 'Y' for this use case.

    These are POLICY SWITCHES — "check this consent before sending" — NOT the channel list.
    A use case can be consent-cleared for a channel and have no traced route at all, or vice versa.
    Never read `checks` as "this use case sends via these channels"; the real channel list is
    `channels_for_use_case()` (tbl_use_case_channel_rule fact).
    """
    manifest = snapshot_manifest()
    needle = (use_case_id or "").strip().lower()
    _dataset, path, rows, cols = _master_rows()
    if not rows:
        return {"use_case_id": use_case_id, "available": False, "source": manifest, "checks": []}
    if not needle:
        return {"use_case_id": use_case_id, "available": True, "source": manifest, "checks": [],
                "error": "use_case_id is required"}
    bound, _ambiguous = _master_column_map(cols)
    id_col = bound.get("use_case_id")
    for line_no, row in rows:
        if not id_col or (row.get(id_col) or "").strip().lower() != needle:
            continue
        checks = []
        for field, label in _CONSENT_FIELD_LABELS.items():
            col = bound.get(field)
            if not col:
                continue
            value = (row.get(col) or "").strip()
            if value.upper() == "Y":
                checks.append({"consent": label, "flag_value": value, "citation": _citation_for(path, line_no)})
        return {"use_case_id": use_case_id, "available": True, "source": manifest, "checks": checks}
    return {"use_case_id": use_case_id, "available": True, "source": manifest, "checks": [],
            "note": "use case not found in tbl_use_case master snapshot"}


# ---------------------------------------------------------------------------
# tbl_use_case_channel_rule / tbl_use_case_ext ingest (Building block 3)
# ---------------------------------------------------------------------------

def rules_by_use_case_id():
    """{uc_id_lower: [rule, …]} from tbl_use_case_channel_rule. Round A = fact only: rule_text is
    NOT parsed here (that is Round B's AST)."""
    dataset = active_dataset()
    path = _table_path(dataset, "tbl_use_case_channel_rule")
    rows, cols = _load_csv(path)
    if not rows:
        return {}
    bound, _ambiguous = _column_map(cols, _RULE_FIELDS)
    id_col = bound.get("use_case_id")
    if not id_col:
        return {}
    out = {}
    for line_no, row in rows:
        uc_id = (row.get(id_col) or "").strip()
        if not uc_id:
            continue
        rule = {
            "channel": _field(row, bound, "channel"),
            "priority": _field(row, bound, "priority"),
            "route": _field(row, bound, "route"),
            "router": _field(row, bound, "router"),
            "traffic_percentage": _field(row, bound, "traffic_percentage"),
            "tag": _field(row, bound, "tag"),
            "sender": _field(row, bound, "sender"),
            "send_policy": _field(row, bound, "send_policy"),
            "status": _field(row, bound, "status"),
            "citation": _citation_for(path, line_no),
        }
        out.setdefault(uc_id.lower(), []).append(rule)
    return out


def ext_by_use_case_id():
    """{uc_id_lower: ext} from tbl_use_case_ext. Missing the Word-only `dormant_period` column is a
    schema-drift signal (unbound field, silently ""), not a crash."""
    dataset = active_dataset()
    path = _table_path(dataset, "tbl_use_case_ext")
    rows, cols = _load_csv(path)
    if not rows:
        return {}
    bound, _ambiguous = _column_map(cols, _EXT_FIELDS)
    id_col = bound.get("use_case_id")
    if not id_col:
        return {}
    out = {}
    for line_no, row in rows:
        uc_id = (row.get(id_col) or "").strip()
        if not uc_id:
            continue
        ext = {field: _field(row, bound, field) for field in _EXT_FIELDS if field != "use_case_id"}
        ext["use_case_id"] = uc_id
        ext["citation"] = _citation_for(path, line_no)
        out[uc_id.lower()] = ext
    return out


def channels_for_use_case(use_case_id):
    """sorted(distinct rule.channel) — the Round A channel FACT, no priority/fallback parsing."""
    rules = rules_by_use_case_id().get((use_case_id or "").strip().lower()) or []
    return sorted({rule["channel"] for rule in rules if rule.get("channel")})


# ---------------------------------------------------------------------------
# source_system canonicalization (Building block 4)
# ---------------------------------------------------------------------------

def _canonical_key(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().casefold())


def _load_aliases():
    try:
        with open(config.SOURCE_SYSTEM_ALIASES_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alias_overrides():
    """canonical_key -> {"display_name", "fold"} built from source_system_aliases.json, e.g.
    {"PEGA": ["Pega","PEGA_HK"]} folds PEGA_HK's canonical key into PEGA's and PEGA becomes the
    preferred display name. Only explicit entries fold — casefold+strip already handles pure
    format/case variance (eAlert/e-Alert/…); this file is for genuinely different-looking names."""
    grouped = {}
    for key, variants in _load_aliases().items():
        canon = _canonical_key(str(key))
        if not canon:
            continue
        fold = {canon}
        for variant in (variants or []):
            vcanon = _canonical_key(str(variant))
            if vcanon:
                fold.add(vcanon)
        entry = grouped.setdefault(canon, {"display_name": str(key).strip(), "fold": set()})
        entry["fold"] |= fold
    resolved = {}
    for entry in grouped.values():
        for member in entry["fold"]:
            resolved[member] = entry
    return resolved


def canonicalize_source_system(raw):
    """{canonical, display_name, raw}. Steps: trim -> casefold -> strip non-alphanumerics for the
    canonical key (folds eAlert/e-Alert/E-alert automatically) -> an alias override may fold
    genuinely different spellings (PEGA/PEGA_HK) and set the preferred display name. `MDC` and
    `MDC Test` are NEVER auto-merged — "mdc" != "mdctest" once the space is stripped."""
    raw = (raw or "").strip()
    canon = _canonical_key(raw)
    override = _alias_overrides().get(canon)
    if override:
        return {"canonical": _canonical_key(override["display_name"]) or canon,
                "display_name": override["display_name"], "raw": raw}
    return {"canonical": canon, "display_name": raw, "raw": raw}


def source_systems():
    """Canonicalized distinct source systems with use-case + active/routed counts, ordered by
    use_case_count desc. `routed_count` is present only when the route dimension is available for
    the active dataset (defect #2 guard — never a number borrowed from a different environment)."""
    _dataset, _path, rows, cols = _master_rows()
    if not rows:
        return []
    bound, _ambiguous = _master_column_map(cols)
    source_col = bound.get("source_system")
    id_col = bound.get("use_case_id")
    status_col = bound.get("status")
    if not source_col:
        return []
    route = route_dimension()
    routed_ids = _routed_use_case_ids() if route["available"] else set()

    groups = {}
    for _line_no, row in rows:
        raw = (row.get(source_col) or "").strip()
        if not raw:
            continue
        info = canonicalize_source_system(raw)
        canon = info["canonical"]
        bucket = groups.setdefault(canon, {
            "display_counts": {}, "raw_variants": set(),
            "use_case_count": 0, "active_count": 0, "routed_count": 0,
        })
        bucket["raw_variants"].add(raw)
        bucket["display_counts"][info["display_name"]] = bucket["display_counts"].get(info["display_name"], 0) + 1
        bucket["use_case_count"] += 1
        status_raw = (row.get(status_col) or "").strip() if status_col else ""
        if not status_col or status_raw.upper() == "Y":
            bucket["active_count"] += 1
        uc_id = (row.get(id_col) or "").strip().lower() if id_col else ""
        if route["available"] and uc_id and uc_id in routed_ids:
            bucket["routed_count"] += 1

    out = []
    for canon, bucket in groups.items():
        display_name = max(bucket["display_counts"], key=lambda name: (bucket["display_counts"][name], name))
        item = {
            "canonical": canon,
            "display_name": display_name,
            "raw_variants": sorted(bucket["raw_variants"]),
            "use_case_count": bucket["use_case_count"],
            "active_count": bucket["active_count"],
            "inactive_count": bucket["use_case_count"] - bucket["active_count"],
        }
        if route["available"]:
            item["routed_count"] = bucket["routed_count"]
        out.append(item)
    return sorted(out, key=lambda item: (-item["use_case_count"], item["display_name"].lower()))


# ---------------------------------------------------------------------------
# endpoint -> repo resolver (Building block 7)
# ---------------------------------------------------------------------------

_VERSION_TOKEN_RE = re.compile(r"^_?v\d+$", re.IGNORECASE)
_REPO_UNIVERSE_CACHE = {}


def _repo_universe():
    path = config.REPOS_TXT
    try:
        stat = os.stat(path)
    except OSError:
        return set(), {}
    sig = (stat.st_mtime, stat.st_size)
    cached = _REPO_UNIVERSE_CACHE.get(path)
    if cached and cached[0] == sig:
        return cached[1], cached[2]
    repos = set()
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                name = line.strip()
                if name:
                    repos.add(name)
    except OSError:
        repos = set()
    normalized = {}
    for repo in repos:
        key = re.sub(r"[-_]", "", repo.lower())
        normalized.setdefault(key, repo)
    _REPO_UNIVERSE_CACHE[path] = (sig, repos, normalized)
    return repos, normalized


def resolve_endpoint(endpoint_raw):
    """[{raw, repo, confidence}] — split an Ext.endpoint `->` chain, skip version tokens
    (v1..v9 / _v4 / standalone), match each remaining segment against `config.REPOS_TXT`.
    Round A: data + confidence only — dynamic arch rendering of these edges is Round B."""
    text = (endpoint_raw or "").strip()
    if not text:
        return []
    repos, normalized = _repo_universe()
    segments = []
    for part in text.split("->"):
        seg = part.strip()
        if not seg:
            continue
        if _VERSION_TOKEN_RE.match(seg):
            segments.append({"raw": seg, "repo": None, "confidence": "version_annotation"})
            continue
        if seg in repos:
            segments.append({"raw": seg, "repo": seg, "confidence": "declared-exact"})
            continue
        key = re.sub(r"[-_]", "", seg.lower())
        match = normalized.get(key)
        if match:
            segments.append({"raw": seg, "repo": match, "confidence": "declared-normalized"})
            continue
        segments.append({"raw": seg, "repo": None, "confidence": "unresolved"})
    return segments


def _entrypoint_traceable(resolved_segments):
    return any(seg["confidence"] in ("declared-exact", "declared-normalized") for seg in resolved_segments)


# ---------------------------------------------------------------------------
# active filter + coverage funnel (Building blocks 5 & 6) + pagination (8)
# ---------------------------------------------------------------------------

def use_cases_for_source_system(source_system, include_inactive=False, offset=0, limit=None):
    """Members of one canonicalized source_system. Defaults to ACTIVE (status=='Y') only; every
    response carries active_count/inactive_count and each item carries active/configured/
    expression_ready/entrypoint_traceable (Round A coverage-funnel flags) plus has_route ONLY when
    a same-environment route snapshot exists (defect #2 guard)."""
    manifest = snapshot_manifest()
    value = (source_system or "").strip()
    empty = {"available": False, "source": manifest, "total": 0, "active_count": 0,
             "inactive_count": 0, "returned": 0, "truncated": False, "items": []}
    if not value:
        return {**empty, "error": "source_system is required"}
    _dataset, path, rows, cols = _master_rows()
    if not rows:
        return empty
    bound, _ambiguous = _master_column_map(cols)
    id_col = bound.get("use_case_id")
    name_col = bound.get("use_case_name")
    project_col = bound.get("project_name")
    source_col = bound.get("source_system")
    status_col = bound.get("status")
    if not source_col:
        return {**empty, "note": "source_system column not found in master snapshot"}

    query_canon = canonicalize_source_system(value)["canonical"]
    route = route_dimension()
    routed_ids = _routed_use_case_ids() if route["available"] else set()
    rules_idx = rules_by_use_case_id()
    ext_idx = ext_by_use_case_id()

    matched = []
    for line_no, row in rows:
        raw_source = (row.get(source_col) or "").strip()
        if not raw_source or canonicalize_source_system(raw_source)["canonical"] != query_canon:
            continue
        status_raw = (row.get(status_col) or "").strip() if status_col else ""
        active = (status_raw.upper() == "Y") if status_col else True
        uc_id = (row.get(id_col) or "").strip() if id_col else ""
        uc_key = uc_id.lower()
        ext = ext_idx.get(uc_key) or {}
        has_rule = bool(rules_idx.get(uc_key))
        endpoint_repos = resolve_endpoint(ext.get("endpoint") or "")
        item = {
            "use_case_id": uc_id,
            "name": (row.get(name_col) or "").strip() if name_col else "",
            "project": (row.get(project_col) or "").strip() if project_col else "",
            "active": active,
            "channels": channels_for_use_case(uc_id) if uc_id else [],
            "configured": has_rule,
            "expression_ready": bool(ext.get("rule_text")),
            "entrypoint_traceable": _entrypoint_traceable(endpoint_repos),
            "catalog_only": not has_rule and uc_key not in ext_idx,
            "endpoint_repos": endpoint_repos,
            "citation": _citation_for(path, line_no),
        }
        if route["available"]:
            item["has_route"] = uc_key in routed_ids
        matched.append(item)

    total = len(matched)
    active_count = sum(1 for item in matched if item["active"])
    inactive_count = total - active_count
    filtered = matched if include_inactive else [item for item in matched if item["active"]]
    offset = max(0, offset or 0)
    window = filtered[offset:offset + limit] if limit else filtered[offset:]
    return {
        "available": True, "source": manifest, "total": total,
        "active_count": active_count, "inactive_count": inactive_count,
        "returned": len(window), "truncated": (offset + len(window)) < len(filtered),
        "items": window,
    }


def source_system_coverage(source_system):
    """UAT-native readiness funnel — replaces the old routed/route-join coverage:
    {canonical, display_name, total, active, configured, expression_ready, entrypoint_traceable,
    catalog_only}. These are STAGES, never "reaches the customer"."""
    members = use_cases_for_source_system(source_system, include_inactive=True)
    info = canonicalize_source_system(source_system)
    items = members.get("items") or []
    configured = sum(1 for item in items if item.get("configured"))
    expression_ready = sum(1 for item in items if item.get("expression_ready"))
    entrypoint_traceable = sum(1 for item in items if item.get("entrypoint_traceable"))
    catalog_only = sum(1 for item in items if item.get("catalog_only"))
    return {
        "canonical": info["canonical"], "display_name": info["display_name"],
        "total": members.get("total", 0), "active": members.get("active_count", 0),
        "configured": configured, "expression_ready": expression_ready,
        "entrypoint_traceable": entrypoint_traceable, "catalog_only": catalog_only,
        "route_dimension": route_dimension(),
    }


def owners_for(use_case_ids):
    """Layered change-notification list (defect #7): `business_owners` (Ext real owners) >
    `cost_governance` (Ext sign-off) > `config_maintainers` (master created_by/modified_by — these
    are MAINTENANCE fields, not business owners). Missing Ext -> only config_maintainers populate."""
    wanted = {str(uid).strip().lower() for uid in use_case_ids if str(uid or "").strip()}
    empty = {"business_owners": [], "cost_governance": [], "config_maintainers": []}
    if not wanted:
        return empty
    _dataset, _path, rows, cols = _master_rows()
    config_maintainers = set()
    if rows:
        bound, _ambiguous = _master_column_map(cols)
        id_col = bound.get("use_case_id")
        if id_col:
            for _line_no, row in rows:
                if (row.get(id_col) or "").strip().lower() not in wanted:
                    continue
                for field in ("created_by", "modified_by"):
                    value = _field(row, bound, field)
                    if value:
                        config_maintainers.add(value)

    business_owners, cost_governance = set(), set()
    ext_idx = ext_by_use_case_id()
    for uc_id in wanted:
        ext = ext_idx.get(uc_id)
        if not ext:
            continue
        for field in ("message_owner", "business_contact", "business_team", "team_head", "depart_head"):
            value = (ext.get(field) or "").strip()
            if value:
                business_owners.add(value)
        for field in ("cost_owner", "signoff_by"):
            value = (ext.get(field) or "").strip()
            if value:
                cost_governance.add(value)

    return {
        "business_owners": sorted(business_owners),
        "cost_governance": sorted(cost_governance),
        "config_maintainers": sorted(config_maintainers),
    }


# ---------------------------------------------------------------------------
# quality report (Building block 10)
# ---------------------------------------------------------------------------

def quality_report(examples_limit=5):
    """Counts + example ids: column bindings (bound + ambiguous), active/inactive, the Round A
    coverage funnel, missing source_system, staleness, illegal business_category codes
    (data-contract drift — must flag e.g. 33/37 on UAT), junk work_stream_name, status uniformity,
    and route-dimension availability. Missing dataset -> available:False, returncode 0."""
    manifest = snapshot_manifest()
    _dataset, path, rows, cols = _master_rows()
    if not rows:
        return {"available": False, "source": manifest, "note": "tbl_use_case master snapshot absent"}

    bound, ambiguous = _master_column_map(cols)
    id_col = bound.get("use_case_id")
    source_col = bound.get("source_system")
    category_col = bound.get("business_category")
    workstream_col = bound.get("work_stream_name")
    status_col = bound.get("status")
    created_col = bound.get("created_time")
    modified_col = bound.get("last_modified_time")

    rules_idx = rules_by_use_case_id()
    ext_idx = ext_by_use_case_id()

    missing_source, stale_ids = [], []
    active_ids, inactive_ids = [], []
    illegal_enum, junk_streams, statuses = {}, {}, {}
    configured_ids, expression_ready_ids, catalog_only_ids = [], [], []
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    total = 0

    for _line_no, row in rows:
        total += 1
        uc = (row.get(id_col) or "").strip() if id_col else ""
        uc_key = uc.lower()
        if source_col and not (row.get(source_col) or "").strip():
            missing_source.append(uc)
        if _is_stale(
            (row.get(modified_col) or "") if modified_col else "",
            (row.get(created_col) or "") if created_col else "",
            reference,
        ):
            stale_ids.append(uc)
        if category_col:
            raw = (row.get(category_col) or "").strip()
            if raw:
                try:
                    code = int(float(raw))
                except ValueError:
                    code = None
                if code is None or code not in BUSINESS_CATEGORY_ENUM:
                    illegal_enum.setdefault(raw, []).append(uc)
        if workstream_col:
            value = (row.get(workstream_col) or "").strip()
            if value.lower() in _JUNK_WORK_STREAM or value.isdigit():
                junk_streams.setdefault(value or "(blank)", []).append(uc)
        if status_col:
            value = (row.get(status_col) or "").strip()
            statuses[value] = statuses.get(value, 0) + 1
            (active_ids if value.upper() == "Y" else inactive_ids).append(uc)
        has_rules = bool(rules_idx.get(uc_key))
        has_ext = uc_key in ext_idx
        if has_rules:
            configured_ids.append(uc)
        if has_ext and (ext_idx[uc_key].get("rule_text") or "").strip():
            expression_ready_ids.append(uc)
        if not has_rules and not has_ext:
            catalog_only_ids.append(uc)

    def sample(ids):
        return ids[:examples_limit]

    def pct(count):
        return round(100 * count / total, 1) if total else 0.0

    return {
        "available": True,
        "source": manifest,
        "row_count": total,
        "column_bindings": {"bound": bound, "ambiguous": ambiguous},
        "route_dimension": route_dimension(),
        "active_inactive": {
            "active": len(active_ids), "inactive": len(inactive_ids),
            "examples": {"active": sample(active_ids), "inactive": sample(inactive_ids)},
        },
        "coverage_funnel": {
            "configured": len(configured_ids),
            "expression_ready": len(expression_ready_ids),
            "catalog_only": len(catalog_only_ids),
            "examples": {
                "configured": sample(configured_ids),
                "expression_ready": sample(expression_ready_ids),
                "catalog_only": sample(catalog_only_ids),
            },
        },
        "missing_source_system": {
            "count": len(missing_source), "pct": pct(len(missing_source)), "examples": sample(missing_source),
        },
        "stale": {
            "count": len(stale_ids), "pct": pct(len(stale_ids)), "examples": sample(stale_ids),
            "note": "last_modified_time (fallback created_time) older than 12 months",
        },
        "illegal_enum": {
            "count": sum(len(v) for v in illegal_enum.values()),
            "codes": sorted(illegal_enum),
            "examples": sample([uc for ids in illegal_enum.values() for uc in ids]),
            "note": "business_category codes not in the seed dict — data-contract drift",
        },
        "junk_work_stream": {
            "count": sum(len(v) for v in junk_streams.values()),
            "values": sorted(junk_streams),
            "examples": sample([uc for ids in junk_streams.values() for uc in ids]),
        },
        "status_uniform": {
            "uniform": len(statuses) <= 1,
            "value": next(iter(statuses), None) if len(statuses) == 1 else None,
            "counts": statuses,
            "note": ("export may be pre-filtered; cannot infer active/inactive"
                      if len(statuses) <= 1 else None),
        },
    }


def render_quality_markdown(report):
    if not report.get("available"):
        return "# Use Case Master — Data Quality\n\n" + (report.get("note") or "unavailable") + "\n"
    funnel = report["coverage_funnel"]
    active_inactive = report["active_inactive"]
    route = report["route_dimension"]
    bindings = report["column_bindings"]
    lines = [
        "# Use Case Master — Data Quality", "",
        f"_source: {report['source'].get('source_table')} ({report['source'].get('environment')}), "
        f"{report['row_count']} rows_", "",
        "## Column bindings",
        f"- bound: {', '.join(sorted(bindings['bound'])) or 'none'}",
        f"- ambiguous (never silently picked): {', '.join(sorted(bindings['ambiguous'])) or 'none'}",
        "",
        "## Active / inactive",
        f"- active: {active_inactive['active']}; inactive: {active_inactive['inactive']}",
        "",
        "## Route dimension",
        f"- available: {route['available']}" + (f" ({route['reason']})" if route.get('reason') else ""),
        "",
        "## Coverage funnel (configured / expression_ready / catalog_only)",
        f"- configured (>=1 channel rule): {funnel['configured']} "
        f"(example ids: {', '.join(funnel['examples']['configured']) or 'none'})",
        f"- expression_ready (non-blank rule_text): {funnel['expression_ready']} "
        f"(example ids: {', '.join(funnel['examples']['expression_ready']) or 'none'})",
        f"- catalog_only (no rule, no ext): {funnel['catalog_only']} "
        f"(example ids: {', '.join(funnel['examples']['catalog_only']) or 'none'})",
        "",
        "## Missing source_system",
        f"- {report['missing_source_system']['count']} rows ({report['missing_source_system']['pct']}%); "
        f"examples: {', '.join(report['missing_source_system']['examples']) or 'none'}",
        "",
        "## Stale (last_modified_time > 12 months)",
        f"- {report['stale']['count']} rows ({report['stale']['pct']}%); "
        f"examples: {', '.join(report['stale']['examples']) or 'none'}",
        "",
        "## Illegal business_category codes (data-contract drift)",
        f"- codes: {', '.join(report['illegal_enum']['codes']) or 'none'}; "
        f"{report['illegal_enum']['count']} rows; examples: {', '.join(report['illegal_enum']['examples']) or 'none'}",
        "",
        "## Junk work_stream_name values",
        f"- values: {', '.join(report['junk_work_stream']['values']) or 'none'}; "
        f"{report['junk_work_stream']['count']} rows",
        "",
        "## status column",
        f"- uniform: {report['status_uniform']['uniform']}"
        + (f" (all {report['status_uniform']['value']!r}) — {report['status_uniform']['note']}"
           if report['status_uniform']['note'] else ""),
    ]
    return "\n".join(lines).rstrip() + "\n"
