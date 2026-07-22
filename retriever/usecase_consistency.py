"""Round B3 — multi-source consistency validator, over the active Round A dataset
(retriever/usecase_catalog.py) plus the Round B1 rule_text structural AST (retriever/rule_text.py).

Compares three DATA-COMPUTABLE sources per use case:
  1. rule_text channel set (B1 AST) vs channel_rule.channel distinct set -> channel_set_mismatch
  2. rule_text structural grouping (B1 stage_groups) vs channel_rule.priority ordering ->
     expression_vs_priority (the I0141/I0142 canonical case: rule_text groups EMAIL & SMS into one
     stage, but priority assigns them distinct numbers 2/3, implying a strict order not a group)
  3. rule_text internal validity: duplicate/unknown channel tokens, blank rule_text with rules present

The Portal composer / runtime parser is a 4th, CODE-ONLY source that cannot be computed per-use-case
from data here — see PORTAL_COMPOSER_CAVEAT. This module never silently picks a winner between
disagreeing sources; every finding carries both sources' citations, severity-ranked.

`quality_findings()` folds in the RUNBOOK-45 Part B categories (orphan channel_rule/ext rows, active
use cases with no channel rule, master rows missing Ext, null priority, PUSH+INBOX unconfigured,
illegal business_category) as GENERIC checks computed live from whatever dataset is active — the
specific ids named in the Part B report (C5501, W9992, ...) were that one UAT snapshot's examples,
never hardcoded here.
"""
from . import rule_text as rt
from . import usecase_catalog as uc

PORTAL_COMPOSER_CAVEAT = (
    "The Portal composer / runtime rule_text parser is a code-only 4th source that cannot be "
    "computed per-use-case from data here. Known divergence (RUNBOOK-45 Part B): for I0141/I0142, "
    "rule_text `LETTER > (EMAIL & SMS)` disagrees with BOTH the channel_rule priority order "
    "(LETTER=1/EMAIL=2/SMS=3) and the Portal composer output (`LETTER > EMAIL > SMS`), and the "
    "runtime parser itself has bugs (e.g. contains(\"\\\\|\") matches a literal backslash, not the "
    "operator). Do not treat the runtime as ground truth."
)

_SEVERITY_RANK = {"error": 3, "warning": 2, "info": 1}

# DB value vs Java enum naming drift (RUNBOOK-45 Part B evidence) — normalize both spellings to one
# key before comparing.
_PUSH_INBOX_KEY = "PUSHINBOX"


def _finding(check, severity, use_case_id, message, citations):
    return {
        "check": check, "severity": severity, "use_case_id": use_case_id, "message": message,
        "citations": sorted({c for c in (citations or []) if c}),
    }


def _priority_groups(rules):
    """channel_rule rows grouped by identical numeric priority, in ascending priority order —
    e.g. LETTER=1,EMAIL=2,SMS=3 -> [["LETTER"], ["EMAIL"], ["SMS"]]. Rows with a blank or
    non-numeric priority are excluded (null_priority is its own separate check)."""
    parsed = []
    for rule in rules:
        channel = (rule.get("channel") or "").strip().upper()
        raw = (rule.get("priority") or "").strip()
        if not channel or not raw:
            continue
        try:
            parsed.append((int(float(raw)), channel))
        except ValueError:
            continue
    if not parsed:
        return []
    parsed.sort(key=lambda item: item[0])
    groups = []
    for priority, channel in parsed:
        if groups and groups[-1][0] == priority:
            groups[-1][1].append(channel)
        else:
            groups.append((priority, [channel]))
    return [sorted(channels) for _priority, channels in groups]


def check_use_case(use_case_id, rules_idx=None, ext_idx=None):
    """Findings for ONE use case, comparing rule_text (B1 AST) against channel_rule facts.
    `rules_idx`/`ext_idx` are optional pre-built {lower_id: ...} indices — pass them when checking
    many use cases in a loop (e.g. from quality_findings()) to avoid rebuilding the full-table
    index on every call. Always returns a list (possibly empty) — never raises."""
    uc_id = (use_case_id or "").strip()
    if not uc_id:
        return []
    key = uc_id.lower()
    rules_idx = uc.rules_by_use_case_id() if rules_idx is None else rules_idx
    ext_idx = uc.ext_by_use_case_id() if ext_idx is None else ext_idx
    rules = rules_idx.get(key) or []
    ext = ext_idx.get(key)
    ext_citation = ext.get("citation") if ext else None
    findings = []

    if not rules and not ext:
        return findings  # nothing to compare — catalog_only, not a consistency question

    rule_text_raw = (ext.get("rule_text") or "").strip() if ext else ""
    ast = rt.parse(rule_text_raw)

    if not rule_text_raw and rules:
        findings.append(_finding(
            "blank_with_rules", "warning", uc_id,
            f"tbl_use_case_ext.rule_text is blank but {len(rules)} channel_rule row(s) exist",
            [ext_citation] + [r.get("citation") for r in rules]))

    for warning in ast["parse_warnings"]:
        if warning["type"] == "duplicate_channel":
            findings.append(_finding(
                "duplicate_channel", "warning", uc_id,
                f"rule_text repeats channel {warning['channel']}: {ast['normalized_expression']!r}",
                [ext_citation]))
        elif warning["type"] == "unknown_channel":
            findings.append(_finding(
                "unknown_channel", "warning", uc_id,
                f"rule_text token {warning['token']!r} is not a known channel",
                [ext_citation]))
        elif warning["type"] in ("unbalanced_parens", "syntax_error", "literal_escape_artifact"):
            findings.append(_finding(
                warning["type"], "error", uc_id,
                f"rule_text {warning['type'].replace('_', ' ')}: {warning.get('detail', '')}",
                [ext_citation]))

    if not rules:
        return findings  # no channel_rule facts to cross-check the expression against

    rule_channels = sorted({(r.get("channel") or "").strip().upper()
                             for r in rules if (r.get("channel") or "").strip()})
    rule_citations = [r.get("citation") for r in rules]

    if ast["channels"] and rule_channels and sorted(ast["channels"]) != rule_channels:
        findings.append(_finding(
            "channel_set_mismatch", "error", uc_id,
            f"rule_text channels {sorted(ast['channels'])} != channel_rule channels {rule_channels}",
            [ext_citation] + rule_citations))
        return findings  # ordering comparison below is meaningless once the sets disagree

    if ast["mode"] in ("FALLBACK", "PARALLEL", "MIXED") and ast["operator_tree"]:
        rule_groups = _priority_groups(rules)
        expr_groups = [sorted(stage) for stage in rt.stage_groups(ast["operator_tree"])]
        if rule_groups and expr_groups and rule_groups != expr_groups:
            findings.append(_finding(
                "expression_vs_priority", "error", uc_id,
                f"rule_text grouping {expr_groups} disagrees with channel_rule priority order "
                f"{rule_groups}",
                [ext_citation] + rule_citations))

    return findings


# ---------------------------------------------------------------------------
# Dataset-wide, severity-ranked findings (folds in the Part B categories, generic)
# ---------------------------------------------------------------------------

def quality_findings(severity=None, offset=0, limit=50):
    """Severity-ranked findings across the whole active dataset: per-use-case consistency
    (check_use_case, for every UC with both a channel_rule and an Ext row) plus the dataset-wide
    categories from RUNBOOK-45 Part B — orphan channel_rule/ext rows, active use cases with no
    channel rule, master rows missing Ext, null priority, PUSH+INBOX with no route/router/Ext, and
    illegal business_category (reusing usecase_catalog.quality_report(), not re-scanned here).

    `counts_by_severity` is always the FULL, unfiltered breakdown (for a dashboard tile);
    `severity` (optional: "error"/"warning"/"info") filters `findings` before `offset`/`limit`
    pagination is applied — `total_findings`/`truncated` describe the (possibly severity-filtered)
    result, not the grand total. `limit=0` means unlimited. Missing dataset -> a clean
    available:False payload, not a crash."""
    manifest = uc.snapshot_manifest()
    _dataset, _path, rows, cols = uc._master_rows()
    if not rows:
        return {"available": False, "source": manifest, "total_findings": 0,
                "counts_by_severity": {}, "findings": [], "returned": 0, "truncated": False,
                "portal_composer_caveat": PORTAL_COMPOSER_CAVEAT}

    bound, _ambiguous = uc._master_column_map(cols)
    id_col = bound.get("use_case_id")
    status_col = bound.get("status")

    active_by_id = {}
    for _line_no, row in rows:
        raw_id = (row.get(id_col) or "").strip() if id_col else ""
        if not raw_id:
            continue
        status_raw = (row.get(status_col) or "").strip() if status_col else ""
        active_by_id[raw_id] = (status_raw.upper() == "Y") if status_col else True
    master_ids_lower = {rid.lower() for rid in active_by_id}

    rules_idx = uc.rules_by_use_case_id()
    ext_idx = uc.ext_by_use_case_id()

    findings = []

    for lower_id in sorted(set(rules_idx) - master_ids_lower):
        cites = [r.get("citation") for r in rules_idx[lower_id]]
        findings.append(_finding(
            "channel_rule_orphan", "warning", lower_id.upper(),
            "channel_rule row(s) reference a use_case_id absent from tbl_use_case", cites))

    for lower_id in sorted(set(ext_idx) - master_ids_lower):
        findings.append(_finding(
            "ext_orphan", "warning", lower_id.upper(),
            "tbl_use_case_ext row references a use_case_id absent from tbl_use_case",
            [ext_idx[lower_id].get("citation")]))

    no_ext_active = no_ext_inactive = 0
    for raw_id, is_active in active_by_id.items():
        key = raw_id.lower()
        rules = rules_idx.get(key) or []
        ext = ext_idx.get(key)

        if is_active and not rules:
            blank_rule_text = bool(ext) and not (ext.get("rule_text") or "").strip()
            detail = (" and blank rule_text" if blank_rule_text else
                      " (no ext row either)" if not ext else "")
            findings.append(_finding(
                "active_no_channel_rule", "error", raw_id,
                f"active use case has zero tbl_use_case_channel_rule rows{detail}",
                [ext.get("citation")] if ext else []))

        if not ext:
            if is_active:
                no_ext_active += 1
            else:
                no_ext_inactive += 1

        if is_active and rules:
            blanks = [r for r in rules if not (r.get("priority") or "").strip()]
            if blanks:
                findings.append(_finding(
                    "null_priority", "error", raw_id,
                    f"{len(blanks)}/{len(rules)} channel_rule row(s) have a blank priority "
                    "(a naive Comparator.comparing without nullsFirst/nullsLast may NPE)",
                    [r.get("citation") for r in blanks]))

            push_inbox_rules = [
                r for r in rules
                if (r.get("channel") or "").strip().upper().replace("_", "").replace("+", "")
                == _PUSH_INBOX_KEY
            ]
            unrouted = [r for r in push_inbox_rules
                        if not (r.get("route") or "").strip() and not (r.get("router") or "").strip()]
            if unrouted and not ext:
                findings.append(_finding(
                    "push_inbox_unconfigured", "warning", raw_id,
                    "PUSH+INBOX channel rule has no route/router and no Ext row — confirm with the "
                    "MDC/runtime owner whether this is a placeholder or missing runtime "
                    "configuration; not this repo's to fix",
                    [r.get("citation") for r in unrouted]))

    if no_ext_active or no_ext_inactive:
        findings.append({
            "check": "master_no_ext", "severity": "warning", "use_case_id": None,
            "message": f"{no_ext_active} active + {no_ext_inactive} inactive use cases have no "
                       "tbl_use_case_ext row",
            "citations": [], "count_active": no_ext_active, "count_inactive": no_ext_inactive,
        })

    base_quality = uc.quality_report()
    if base_quality.get("available"):
        for code in base_quality["illegal_enum"]["codes"]:
            findings.append({
                "check": "illegal_business_category", "severity": "warning", "use_case_id": None,
                "message": f"business_category code {code} is not in the known enum "
                           "(data-contract drift) — official name/topic pending owner confirmation",
                "citations": [], "code": code,
            })

    for lower_id in sorted(set(rules_idx) & set(ext_idx)):
        findings.extend(check_use_case(lower_id.upper(), rules_idx=rules_idx, ext_idx=ext_idx))

    findings.sort(key=lambda f: (-_SEVERITY_RANK.get(f["severity"], 0), f["check"],
                                  f.get("use_case_id") or ""))
    counts_by_severity = {}
    for f in findings:
        counts_by_severity[f["severity"]] = counts_by_severity.get(f["severity"], 0) + 1

    filtered = [f for f in findings if f["severity"] == severity] if severity else findings
    total = len(filtered)
    offset = max(0, offset or 0)
    window = filtered[offset:offset + limit] if limit else filtered[offset:]
    return {
        "available": True, "source": manifest,
        "total_findings": total,
        "counts_by_severity": counts_by_severity,
        "findings": window,
        "returned": len(window),
        "truncated": (offset + len(window)) < total,
        "portal_composer_caveat": PORTAL_COMPOSER_CAVEAT,
    }
