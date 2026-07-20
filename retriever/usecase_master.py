"""Read-only ingest + join for the Use Case master data (tbl_use_case.csv, dev/SCT snapshot).

This is a SECOND snapshot on the same use_case_id primary key as the routing snapshot in
``retriever/messages.py``. It supplies identity/governance plus the upstream `source_system`
that appears nowhere else in the codebase — the left end of the chain:

    source_system -> Use Case (identity+policy) -> topic -> producer/consumer repo -> channel -> vendor

Tier 0 only: the real channel chain, priority, and bounce-back fallback live in
tbl_use_case_channel_rule (not yet available) — do NOT derive a channel list from the opt-in
flags here; see consent_preflight()'s docstring.

Missing snapshot file -> available:False / empty results everywhere, never crash."""
import csv
import json
import os
import re
from datetime import datetime, timezone

from . import config, messages

# Seed dict for business_category — source of truth is BusinessCategoryEnum.java on the mirror.
# Any CSV code missing from this dict (e.g. 33) is a data-contract drift alarm, not a bug here.
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

_FIELD_NEEDLES = {
    "use_case_id": ("usecase", "id"),
    "use_case_name": ("usecase", "name"),
    "project_name": ("project",),
    "source_system": ("source", "system"),
    "work_stream_name": ("workstream",),
    "line_of_business": ("lineofbusiness",),
    "business_category": ("business", "category"),
    "country_code": ("country",),
    "group_member": ("group", "member"),
    "app_name": ("app", "name"),
    "created_by": ("created", "by"),
    "created_time": ("created", "time"),
    "modified_by": ("modified", "by"),
    "last_modified_time": ("last", "modified", "time"),
    "status": ("status",),
}

# Consent/opt-in flags — matched on the FULL flattened column name (not a fuzzy needle set)
# because several real columns are near-duplicates (push_optin_flag vs marketing_push_optin_flag
# vs high_risk_push_optin_flag vs securities_push_optin_flag vs marketing_insights_push_optin_flag)
# and a substring match would confuse them.
_CONSENT_LABELS = {
    "marketingoptinflag": "Marketing Consent",
    "pushoptinflag": "Push",
    "marketingpushoptinflag": "Marketing Push",
    "highriskpushoptinflag": "High-Risk Push",
    "securitiespushoptinflag": "Securities Push",
    "marketinginsightspushoptinflag": "Marketing Insights Push",
    "smsoptinflag": "SMS",
    "marketingsmsoptinflag": "Marketing SMS",
    "emailoptinflag": "Email",
    "marketingemailoptinflag": "Marketing Email",
    "mmsoptinflag": "MMS",
    "marketingmmsoptinflag": "Marketing MMS",
    "wechatoptinflag": "WeChat",
    "marketingwechatoptinflag": "Marketing WeChat",
    "whatsappoptinflag": "WhatsApp",
    "marketingwhatsappoptinflag": "Marketing WhatsApp",
}

_JUNK_WORK_STREAM = {"invalid", "test", "n/a", "na", "null", "none", "-", "tbd", "xxx", "unknown", ""}
_STALE_DAYS = 365
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                 "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y")


def _flat(col):
    return re.sub(r"[^a-z0-9]", "", (col or "").lower())


def _detect(cols, *needles):
    for c in cols:
        f = _flat(c)
        if all(n in f for n in needles):
            return c
    return None


def _column_map(cols):
    return {field: _detect(cols, *needles) for field, needles in _FIELD_NEEDLES.items()}


def _consent_columns(cols):
    """{actual_column: label} for whichever consent columns are present in this export."""
    found = {}
    for col in cols:
        label = _CONSENT_LABELS.get(_flat(col))
        if label:
            found[col] = label
    return found


def _load_rows():
    """[(line_no, row), ...], cols — line_no is 1-based (row 2 = first data row).
    Missing file -> ([], []), never crash."""
    try:
        with open(config.USECASE_MASTER_CSV, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            cols = reader.fieldnames or []
            return [(line_no, row) for line_no, row in enumerate(reader, 2)], cols
    except FileNotFoundError:
        return [], []


def _citation(line_no):
    try:
        path = os.path.relpath(config.USECASE_MASTER_CSV, config.ROOT)
    except ValueError:
        path = config.USECASE_MASTER_CSV
    return path.replace(os.sep, "/") + ":" + str(line_no)


def snapshot_manifest():
    """Provenance envelope — same shape as messages._snapshot_manifest(), source_table swapped,
    so no consumer can present this as production truth."""
    path = config.USECASE_MASTER_CSV
    manifest = {
        "environment": "dev/SCT",
        "source_table": "tbl_use_case",
        "exported_at": None,
        "row_count": 0,
        "production_verified": False,
        "caveat": ("dev/SCT snapshot — indicative, NOT production. Absence here does not prove "
                   "absence in production."),
    }
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


def _load_aliases():
    try:
        with open(config.SOURCE_SYSTEM_ALIASES_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alias_group(value):
    """{value} folded through source_system_aliases.json (e.g. {"PEGA": ["Pega","PEGA_HK"]}),
    case-insensitive. No alias file, or value not listed -> just {value}."""
    needle = (value or "").strip().lower()
    for canonical, alist in _load_aliases().items():
        names = {str(canonical).strip().lower()} | {str(a).strip().lower() for a in (alist or [])}
        if needle in names:
            return names
    return {needle}


def _routed_use_case_ids():
    """Lowercased use_case_ids that appear in the routing snapshot (has a known topic route)."""
    rows, cols = messages._load_usecase()
    uc_col, _tp = messages._usecase_columns(cols)
    if not uc_col:
        return set()
    return {(row.get(uc_col) or "").strip().lower() for row in rows if (row.get(uc_col) or "").strip()}


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
    """True/False, or None if neither date is parseable (unknown, not counted either way)."""
    reference = reference or datetime.now(timezone.utc).replace(tzinfo=None)
    dt = _parse_dt(last_modified_time) or _parse_dt(created_time)
    if dt is None:
        return None
    return (reference - dt).days > _STALE_DAYS


def is_stale(last_modified_time, created_time=""):
    """Public wrapper: True/False whether last_modified_time (falling back to created_time) is
    older than 12 months. None if neither date is parseable — unknown, not asserted either way."""
    return _is_stale(last_modified_time, created_time)


def _field(row, cmap, name):
    col = cmap.get(name)
    return (row.get(col) or "").strip() if col else ""


def _category_label(code_raw):
    if not code_raw:
        return "UNKNOWN()"
    try:
        code = int(float(code_raw))
    except ValueError:
        return f"UNKNOWN({code_raw})"
    return BUSINESS_CATEGORY_ENUM.get(code) or f"UNKNOWN({code})"


def _identity(line_no, row, cmap):
    code_raw = _field(row, cmap, "business_category")
    return {
        "use_case_id": _field(row, cmap, "use_case_id"),
        "name": _field(row, cmap, "use_case_name"),
        "project": _field(row, cmap, "project_name"),
        "source_system": _field(row, cmap, "source_system"),
        "work_stream": _field(row, cmap, "work_stream_name"),
        "line_of_business": _field(row, cmap, "line_of_business"),
        "business_category_code": code_raw,
        "business_category_label": _category_label(code_raw),
        "country": _field(row, cmap, "country_code"),
        "group_member": _field(row, cmap, "group_member"),
        "app": _field(row, cmap, "app_name"),
        "created_by": _field(row, cmap, "created_by"),
        "created_time": _field(row, cmap, "created_time"),
        "modified_by": _field(row, cmap, "modified_by"),
        "last_modified_time": _field(row, cmap, "last_modified_time"),
        "status": _field(row, cmap, "status"),
        "citation": _citation(line_no),
    }


def master_for(use_case_id):
    """The joined Use Case identity for one id, or None if absent from the master snapshot
    (missing entirely, or the file itself is absent)."""
    needle = (use_case_id or "").strip().lower()
    if not needle:
        return None
    rows, cols = _load_rows()
    if not rows:
        return None
    cmap = _column_map(cols)
    id_col = cmap.get("use_case_id")
    if not id_col:
        return None
    for line_no, row in rows:
        if (row.get(id_col) or "").strip().lower() == needle:
            return _identity(line_no, row, cmap)
    return None


def consent_preflight(use_case_id):
    """Pre-send consent/opt-in switches that are 'Y' for this use case.

    These are POLICY SWITCHES — "check this consent before sending" — NOT the channel list.
    A use case can be consent-cleared for a channel and have no traced route at all (Tier 0 has
    no tbl_use_case_channel_rule yet), or vice versa. Never read `checks` as "this use case sends
    via these channels"; that answer waits on Tier 1.
    """
    manifest = snapshot_manifest()
    needle = (use_case_id or "").strip().lower()
    rows, cols = _load_rows()
    if not rows:
        return {"use_case_id": use_case_id, "available": False, "source": manifest, "checks": []}
    if not needle:
        return {"use_case_id": use_case_id, "available": True, "source": manifest, "checks": [],
                "error": "use_case_id is required"}
    cmap = _column_map(cols)
    id_col = cmap.get("use_case_id")
    consent_cols = _consent_columns(cols)
    for line_no, row in rows:
        if not id_col or (row.get(id_col) or "").strip().lower() != needle:
            continue
        checks = []
        for col, label in consent_cols.items():
            value = (row.get(col) or "").strip()
            if value.upper() == "Y":
                checks.append({"consent": label, "flag_value": value, "citation": _citation(line_no)})
        return {"use_case_id": use_case_id, "available": True, "source": manifest, "checks": checks}
    return {"use_case_id": use_case_id, "available": True, "source": manifest, "checks": [],
            "note": "use case not found in tbl_use_case master snapshot"}


def use_cases_for_source_system(source_system, limit=None):
    """Members of one source_system, split by has_route so a blast radius never pads in the
    catalog-only (no traced channel) members as if they were traceable."""
    manifest = snapshot_manifest()
    value = (source_system or "").strip()
    empty = {"available": False, "source": manifest, "total": 0, "returned": 0,
             "truncated": False, "items": []}
    if not value:
        return {**empty, "error": "source_system is required"}
    rows, cols = _load_rows()
    if not rows:
        return empty
    cmap = _column_map(cols)
    id_col = cmap.get("use_case_id")
    name_col = cmap.get("use_case_name")
    project_col = cmap.get("project_name")
    source_col = cmap.get("source_system")
    if not source_col:
        return {**empty, "note": "source_system column not found in master snapshot"}

    group = _alias_group(value)
    routed_ids = _routed_use_case_ids()
    items = []
    for line_no, row in rows:
        if (row.get(source_col) or "").strip().lower() not in group:
            continue
        uc_id = (row.get(id_col) or "").strip() if id_col else ""
        items.append({
            "use_case_id": uc_id,
            "name": (row.get(name_col) or "").strip() if name_col else "",
            "project": (row.get(project_col) or "").strip() if project_col else "",
            "has_route": uc_id.lower() in routed_ids,
            "citation": _citation(line_no),
        })
    total = len(items)
    limited = items[:limit] if limit and limit > 0 else items
    return {"available": True, "source": manifest, "total": total, "returned": len(limited),
            "truncated": total > len(limited), "items": limited}


def source_systems():
    """Distinct source systems with their use-case + routed counts, for the source-system picker
    and the arch upstream nodes. Ordered by use_case_count desc so the top entries (PEGA/MDC/…)
    lead."""
    rows, cols = _load_rows()
    if not rows:
        return []
    cmap = _column_map(cols)
    source_col = cmap.get("source_system")
    id_col = cmap.get("use_case_id")
    if not source_col:
        return []
    routed_ids = _routed_use_case_ids()
    counts, routed_counts = {}, {}
    for _line_no, row in rows:
        value = (row.get(source_col) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
        uc_id = (row.get(id_col) or "").strip().lower() if id_col else ""
        if uc_id and uc_id in routed_ids:
            routed_counts[value] = routed_counts.get(value, 0) + 1
    return [
        {"source_system": value, "use_case_count": counts[value], "routed_count": routed_counts.get(value, 0)}
        for value in sorted(counts, key=lambda v: (-counts[v], v.lower()))
    ]


def owners_for(use_case_ids):
    """Distinct non-empty created_by/modified_by across a set of use_case_ids — one CSV pass.
    This is the change-notification list: who to tell when one of these use cases changes."""
    wanted = {str(uid).strip().lower() for uid in use_case_ids if str(uid or "").strip()}
    if not wanted:
        return []
    rows, cols = _load_rows()
    if not rows:
        return []
    cmap = _column_map(cols)
    id_col = cmap.get("use_case_id")
    if not id_col:
        return []
    owners = set()
    for _line_no, row in rows:
        if (row.get(id_col) or "").strip().lower() not in wanted:
            continue
        for name in (_field(row, cmap, "created_by"), _field(row, cmap, "modified_by")):
            if name:
                owners.add(name)
    return sorted(owners)


def quality_report(examples_limit=5):
    """Counts + example ids for the data-quality checks called out in the spec: join coverage,
    missing source_system, staleness, illegal business_category codes (data-contract drift, e.g.
    33), junk work_stream_name values, and status-column uniformity. Missing snapshot -> a clean
    available:False payload, not a crash (refresh.py treats this as returncode 0)."""
    manifest = snapshot_manifest()
    rows, cols = _load_rows()
    if not rows:
        return {"available": False, "source": manifest, "note": "tbl_use_case master snapshot absent"}

    cmap = _column_map(cols)
    id_col = cmap.get("use_case_id")
    source_col = cmap.get("source_system")
    category_col = cmap.get("business_category")
    workstream_col = cmap.get("work_stream_name")
    status_col = cmap.get("status")
    created_col = cmap.get("created_time")
    modified_col = cmap.get("last_modified_time")

    master_ids = {(row.get(id_col) or "").strip().lower() for _ln, row in rows
                  if id_col and (row.get(id_col) or "").strip()}
    routed_ids = _routed_use_case_ids()
    both = sorted(master_ids & routed_ids)
    routing_only = sorted(routed_ids - master_ids)
    master_only = sorted(master_ids - routed_ids)

    missing_source, stale_ids = [], []
    illegal_enum, junk_streams, statuses = {}, {}, {}
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    total = 0

    for _line_no, row in rows:
        total += 1
        uc = (row.get(id_col) or "").strip() if id_col else ""
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

    def sample(ids):
        return ids[:examples_limit]

    def pct(count):
        return round(100 * count / total, 1) if total else 0.0

    return {
        "available": True,
        "source": manifest,
        "row_count": total,
        "join_coverage": {
            "master_and_routing": len(both),
            "routing_only_orphans": len(routing_only),
            "master_only_no_route": len(master_only),
            "examples": {
                "master_and_routing": sample(both),
                "routing_only_orphans": sample(routing_only),
                "master_only_no_route": sample(master_only),
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
    join = report["join_coverage"]
    lines = [
        "# Use Case Master — Data Quality", "",
        f"_source: {report['source'].get('source_table')} ({report['source'].get('environment')}), "
        f"{report['row_count']} rows_", "",
        "## Join coverage (master x routing snapshot, on use_case_id)",
        f"- master ∩ routing: {join['master_and_routing']} (example ids: {', '.join(join['examples']['master_and_routing']) or 'none'})",
        f"- routing-only orphans (route with no master row): {join['routing_only_orphans']} "
        f"(example ids: {', '.join(join['examples']['routing_only_orphans']) or 'none'})",
        f"- master-only, no route: {join['master_only_no_route']} "
        f"(example ids: {', '.join(join['examples']['master_only_no_route']) or 'none'})",
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
