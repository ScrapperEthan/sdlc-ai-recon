#!/usr/bin/env python3
"""Ingest the MDC repo-list sheet -> additive per-repo metadata + the authoritative MDC roster.

CODEX-OWNED INGESTION ADAPTER
-----------------------------
Routine maintenance when the sheet changes = edit ``mdc_sheet_schema.json`` (column names,
new channel/flag columns, value maps). You should NOT need to touch this file for a column
rename or a new flag: the engine resolves a column by exact -> alias -> unique-fuzzy match,
auto-captures ANY yes/no column as a generic flag, and only the Repository column is
mandatory. It never raises on an unrecognized column — unbound semantic fields are reported,
not fatal.

Contract emitted (this is what the consumption layer relies on; keep it stable):
  repo_tags.mdc.json : { "<repo>": { mdc_common, time_critical, marketing_servicing,
                          mode_declared, business_line, channel_declared[], flags{}, attrs{} } }
                       -- make_repo_tags.merge_mdc / retriever.repo_tags read the first six keys.
  mdc_roster.json    : { source, count, repos[] } -- the in-scope MDC membership. "整库=MDC":
                       every repo the sheet lists is MDC; the consumption layer scopes
                       list_repos/search to this set, so amet-* / anything absent is out-of-scope.

The reconcile()/markdown_report() section near the bottom is CONSUMPTION-side QA (it compares
the sheet against Claude-owned name-derived tags in repo_tags.json). It is colocated so the
refresh step can emit a review report, but it is NOT part of the codex ingestion contract —
leave it to the retrieval side.
"""
import argparse
import json
import os
import posixpath
import zipfile
import xml.etree.ElementTree as ET

from retriever import config


NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pkg": "http://schemas.openxmlformats.org/package/2006/relationships"}

# Built-in fallback = the v0.2 sheet layout. Used verbatim when mdc_sheet_schema.json is absent,
# so behaviour is identical to before this file became schema-driven. A committed
# mdc_sheet_schema.json (equal to this) is the file Codex actually edits.
DEFAULT_SCHEMA = {
    "sheet_name": "full Repository List",
    "repository": {"aliases": ["Repository", "Repo", "Repository Name"], "needles": ["repo"]},
    "boolean_true": ["y", "yes", "true", "1", "x"],
    "boolean_false": ["n", "no", "false", "0", ""],
    "channel_flags": {
        "SMS": "sms", "EMAIL": "email", "PUSH": "push", "WhatsAPP": "whatsapp",
        "Letter": "letter", "Wechat": "wechat", "Others": "other",
    },
    "named_flags": {"mdc_common": "MDC Common", "time_critical": "TimeCritcal(Y/N)"},
    "enum_columns": {
        "mode_declared": {"aliases": ["Batch/Realtime(B/R)"], "map": {"R": "realtime", "B": "batch"}},
        "marketing_servicing": {"aliases": ["Maraketing/Servicing(M/S)"], "map": {"M": "marketing", "S": "servicing"}},
        "business_line": {"aliases": ["CMB/WPB"], "allowed": ["cmb", "wpb"]},
    },
    "sensitive_columns": ["Remark"],
}


# ---------------------------------------------------------------------------
# XLSX reading (stdlib only) + schema-driven column resolution
# ---------------------------------------------------------------------------

def _text(node):
    return "".join(node.itertext()) if node is not None else ""


def _column(cell_ref):
    return "".join(char for char in cell_ref if char.isalpha())


def _norm(text):
    """Fold a header/value to a match key: lowercase, alphanumerics only.

    So 'MDC Common', 'mdc_common' and 'MDC-COMMON' all collapse to 'mdccommon'. A real rename
    (different words) will NOT collapse to the same key — that is what the schema aliases are for.
    """
    return "".join(char for char in str(text).lower() if char.isalnum())


def _shared_strings(archive):
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [_text(item) for item in root.findall("main:si", NS)]


def _sheet_path(archive, sheet_name):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels.findall("pkg:Relationship", NS)}
    sheets = workbook.findall("main:sheets/main:sheet", NS)
    chosen = next((s for s in sheets if s.get("name") == sheet_name), None)
    if chosen is None and sheets:
        chosen = sheets[0]  # tolerate a renamed tab rather than crash
    if chosen is None:
        raise ValueError("XLSX has no worksheets")
    target = targets.get(chosen.get("{%s}id" % NS["rel"]))
    if not target:
        raise ValueError("XLSX worksheet relationship is missing")
    return posixpath.normpath(posixpath.join("xl", target.lstrip("/")))


def _cell_value(cell, strings):
    kind = cell.get("t")
    if kind == "inlineStr":
        return _text(cell.find("main:is", NS)).strip()
    value = _text(cell.find("main:v", NS)).strip()
    if kind == "s" and value:
        return strings[int(value)].strip()
    return value


def _row_values(row, strings):
    return {_column(cell.get("r", "")): _cell_value(cell, strings) for cell in row.findall("main:c", NS)}


def load_schema(path=None):
    """Codex-editable column map, falling back to DEFAULT_SCHEMA. A supplied block REPLACES the
    default block of the same name (no deep merge) so the effect of an override is predictable."""
    loaded = load_json(path or config.MDC_SHEET_SCHEMA_JSON)
    schema = dict(DEFAULT_SCHEMA)
    if isinstance(loaded, dict):
        for key, value in loaded.items():
            if key.startswith("_"):  # _README and friends are documentation, not config
                continue
            schema[key] = value
    return schema


def _resolve_column(norm_to_col, aliases, needles=()):
    """exact/alias -> unique-fuzzy(needles) -> None. Never guesses when >1 column could match."""
    for alias in aliases or ():
        col = norm_to_col.get(_norm(alias))
        if col:
            return col
    if needles:
        wanted = [_norm(n) for n in needles]
        hits = [col for norm, col in norm_to_col.items() if all(w in norm for w in wanted)]
        if len(hits) == 1:
            return hits[0]
    return None


def resolve_bindings(norm_to_col, schema):
    """Map the schema's semantic fields onto actual sheet columns. Anything that fails to bind is
    reported in ``unbound`` (a warning surface for Codex), never an error."""
    repo_spec = schema.get("repository", {})
    repo_col = _resolve_column(norm_to_col, repo_spec.get("aliases"), repo_spec.get("needles"))

    channel_cols, named_cols, enum_cols, sensitive_cols, unbound = {}, {}, {}, set(), []

    for header, canonical in (schema.get("channel_flags") or {}).items():
        col = _resolve_column(norm_to_col, [header])
        if col:
            channel_cols[col] = canonical
        else:
            unbound.append(f"channel:{header}")

    for field, header in (schema.get("named_flags") or {}).items():
        col = _resolve_column(norm_to_col, [header])
        if col:
            named_cols[field] = col
        else:
            unbound.append(f"named:{field}")

    for field, spec in (schema.get("enum_columns") or {}).items():
        col = _resolve_column(norm_to_col, spec.get("aliases"))
        if col:
            enum_cols[field] = (col, spec)
        else:
            unbound.append(f"enum:{field}")

    for header in (schema.get("sensitive_columns") or []):
        col = _resolve_column(norm_to_col, [header])
        if col:
            sensitive_cols.add(col)

    return {
        "repository": repo_col,
        "channels": channel_cols,
        "named": named_cols,
        "enums": enum_cols,
        "sensitive": sensitive_cols,
        "unbound": unbound,
    }


def _truthy(value, true_set):
    return _norm(value) in true_set


def _enum_value(raw, spec):
    raw = (raw or "").strip()
    if "map" in spec:
        return spec["map"].get(raw.upper(), spec["map"].get(raw, ""))
    allowed = {str(a).lower() for a in spec.get("allowed", [])}
    low = raw.lower()
    return low if low in allowed else ""


def parse_sheet(path, schema=None):
    """Return (metadata_by_normalized_repo, source_row_by_repo) using only stdlib.

    Each entry keeps the six legacy fields the consumption layer reads, PLUS a generic ``flags``
    map (every yes/no column, so new flags are never lost) and ``attrs`` (any other free-value
    column). Only a missing Repository column is fatal.
    """
    schema = schema or load_schema()
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        root = ET.fromstring(archive.read(_sheet_path(archive, schema.get("sheet_name", ""))))
    rows = root.findall("main:sheetData/main:row", NS)
    if not rows:
        return {}, {}

    header_cells = _row_values(rows[0], strings)
    norm_to_col, col_to_header = {}, {}
    for col, header in header_cells.items():
        header = header.strip()
        if header:
            norm_to_col[_norm(header)] = col
            col_to_header[col] = header

    bind = resolve_bindings(norm_to_col, schema)
    repo_col = bind["repository"]
    if not repo_col:
        raise ValueError(
            "MDC sheet: no Repository column found (looked for %s). Fix mdc_sheet_schema.json."
            % ((schema.get("repository") or {}).get("aliases"))
        )

    true_set = {_norm(v) for v in schema.get("boolean_true", [])}
    bool_domain = true_set | {_norm(v) for v in schema.get("boolean_false", [])}

    data_rows = [( _row_values(row, strings), int(row.get("r", "0") or 0)) for row in rows[1:]]

    # Classify the leftover columns (not repo/sensitive/enum) as yes-no flags vs free-value.
    claimed = {repo_col} | bind["sensitive"] | {col for col, _ in bind["enums"].values()}
    auto_cols = [col for col in col_to_header if col not in claimed]
    bool_cols = []
    for col in auto_cols:
        col_values = [row.get(col, "") for row, _ in data_rows if (row.get(col, "") or "").strip()]
        if all(_norm(v) in bool_domain for v in col_values):  # empty column -> vacuously boolean
            bool_cols.append(col)
    value_cols = [col for col in auto_cols if col not in bool_cols]

    output, source_rows = {}, {}
    for values, rownum in data_rows:
        repo = (values.get(repo_col, "") or "").strip().lower()
        if not repo:
            continue
        channel_declared = sorted({
            canonical for col, canonical in bind["channels"].items()
            if _truthy(values.get(col, ""), true_set)
        })
        entry = {
            "mdc_common": _truthy(values.get(bind["named"].get("mdc_common"), ""), true_set)
            if "mdc_common" in bind["named"] else False,
            "time_critical": _truthy(values.get(bind["named"].get("time_critical"), ""), true_set)
            if "time_critical" in bind["named"] else False,
            "marketing_servicing": "",
            "mode_declared": "",
            "business_line": "",
            "channel_declared": channel_declared,
            "flags": {
                _norm(col_to_header[col]): _truthy(values.get(col, ""), true_set) for col in bool_cols
            },
            "attrs": {
                _norm(col_to_header[col]): (values.get(col, "") or "").strip()
                for col in value_cols if (values.get(col, "") or "").strip()
            },
        }
        for field, (col, spec) in bind["enums"].items():
            entry[field] = _enum_value(values.get(col, ""), spec)
        output[repo] = entry
        source_rows[repo] = rownum
    return output, source_rows


# ---------------------------------------------------------------------------
# Artifacts (ingestion output — the codex contract)
# ---------------------------------------------------------------------------

def build_roster(output, sheet_path):
    """The authoritative in-scope MDC roster: every repo the sheet lists. Consumption scopes to it."""
    return {
        "source": os.path.basename(sheet_path),
        "count": len(output),
        "repos": sorted(output.keys()),
    }


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(payload, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def coverage_rows(payload):
    return [
        ("repos_seen", len(payload)),
        ("mdc_common_set", sum(1 for item in payload.values() if item["mdc_common"])),
        ("marketing_servicing_set", sum(1 for item in payload.values() if item["marketing_servicing"])),
        ("time_critical_set", sum(1 for item in payload.values() if item["time_critical"])),
        ("business_line_set", sum(1 for item in payload.values() if item["business_line"])),
        ("channel_declared_set", sum(1 for item in payload.values() if item["channel_declared"])),
        ("fixed_channel_declared_set", sum(1 for item in payload.values() if set(item["channel_declared"]) - {"other"})),
        ("mode_declared_set", sum(1 for item in payload.values() if item["mode_declared"])),
    ]


def print_coverage(payload, unbound=None):
    print("MDC repo metadata coverage\n")
    print(f"{'metric':28} {'count':>6}")
    print("-" * 35)
    for label, count in coverage_rows(payload):
        print(f"{label:28} {count:6d}")
    if unbound:
        print("\nUnbound schema fields (edit mdc_sheet_schema.json if a column was renamed):")
        for name in unbound:
            print(f"  - {name}")


# ---------------------------------------------------------------------------
# CONSUMPTION-side QA (Claude-owned): reconcile the sheet vs name-derived tags.
# Not part of the codex ingestion contract; reads repo_tags.json (structural).
# ---------------------------------------------------------------------------

def reconcile(mdc, source_rows, tags):
    tags = {str(repo).strip().lower(): tag for repo, tag in tags.items()}
    mismatches = []
    confirmations = 0
    for repo, sheet in sorted(mdc.items()):
        tag = tags.get(repo, {})
        structural_channel = sorted(tag.get("channel") or [])
        declared_channel = sorted(sheet["channel_declared"])
        structural_mode = tag.get("mode") or ""
        declared_mode = sheet["mode_declared"]
        # Existing tags also use role suffixes (api/job/lib) as a useful mode-like
        # classifier; the MDC column only speaks R/B, so reconcile only comparable modes.
        comparable_mode = structural_mode if structural_mode in {"realtime", "batch"} else ""
        matches_channel = bool(structural_channel) and structural_channel == declared_channel
        matches_mode = bool(comparable_mode) and comparable_mode == declared_mode
        confirmations += int(matches_channel) + int(matches_mode)
        if (structural_channel and structural_channel != declared_channel) or (
            comparable_mode and comparable_mode != declared_mode
        ):
            mismatches.append({
                "repo": repo, "sheet_row": source_rows.get(repo, 0),
                "structural_channel": structural_channel, "declared_channel": declared_channel,
                "structural_mode": structural_mode, "declared_mode": declared_mode,
                "citation": f"MDC sheet:full Repository List row {source_rows.get(repo, 0)}",
            })
    unknown = [
        (repo, tag) for repo, tag in sorted(tags.items()) if not tag.get("channel")
    ]
    explained = [
        repo for repo, tag in unknown
        if tag.get("mdc_common") or "other" in (mdc.get(repo, {}).get("channel_declared") or []) or tag.get("serves_channels")
    ]
    return {
        "summary": {
            "repos_tagged": len(tags), "channel_unknown": len(unknown),
            "confirmations": confirmations, "mismatches": len(mismatches),
            "explained_unknowns": len(explained), "true_dark": len(unknown) - len(explained),
        },
        "mismatches": mismatches,
        "explained_unknowns": [
            {
                "repo": repo,
                "mdc_common": bool(tags[repo].get("mdc_common")),
                "channel_declared": mdc.get(repo, {}).get("channel_declared", []),
                "serves_channels": tags[repo].get("serves_channels", []),
                "citation": (
                    f"MDC sheet:full Repository List row {source_rows[repo]}"
                    if repo in source_rows else "recon_out/internal_edges.csv: transitive dependent graph"
                ),
            }
            for repo in explained
        ],
        "honesty": [
            "The MDC sheet does not raise fixed-channel ownership coverage; it adds business metadata and reconciliation evidence.",
            "serves_channels is graph blast-radius, not a claim that the repo owns a delivery channel.",
            "No vendor data is present in the MDC sheet; vendor precision remains dependent on delivery topology and future message-map work.",
            "Remark is intentionally excluded because it is free-text and potentially sensitive.",
        ],
    }


def markdown_report(report):
    summary = report["summary"]
    lines = ["# Repository tag reconciliation", "", "## Summary", ""]
    lines += [f"- **{key.replace('_', ' ')}:** {value}" for key, value in summary.items()]
    lines += ["", "## Mismatches", ""]
    if report["mismatches"]:
        lines += ["| Repo | Name-derived | Sheet-declared | Citation |", "| --- | --- | --- | --- |"]
        for item in report["mismatches"]:
            structural = f"channel={item['structural_channel']}; mode={item['structural_mode'] or '-'}"
            declared = f"channel={item['declared_channel']}; mode={item['declared_mode'] or '-'}"
            lines.append(f"| `{item['repo']}` | {structural} | {declared} | {item['citation']} |")
    else:
        lines.append("No mismatches found.")
    lines += ["", "## Explained channel-unknown repos", ""]
    lines.append("These are explained by shared-component metadata, an `Others` declaration, or graph blast-radius; they remain channel-unknown owners.")
    for item in report["explained_unknowns"]:
        lines.append(f"- `{item['repo']}` — mdc_common={item['mdc_common']}, declared={item['channel_declared']}, serves={item['serves_channels']} ({item['citation']})")
    lines += ["", "## Limits", ""] + [f"- {item}" for item in report["honesty"]]
    return "\n".join(lines) + "\n"


def write_text(text, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", default=config.MDC_SHEET_XLSX)
    parser.add_argument("--schema", default=config.MDC_SHEET_SCHEMA_JSON)
    parser.add_argument("--out", default=config.REPO_TAGS_MDC_JSON)
    parser.add_argument("--roster", default=config.MDC_ROSTER_JSON)
    parser.add_argument("--report", action="store_true", help="write reconciliation Markdown and JSON")
    parser.add_argument("--tags", default=config.REPO_TAGS_JSON)
    parser.add_argument("--report-md", default=config.TAG_RECONCILE_MD)
    parser.add_argument("--report-json", default=config.TAG_RECONCILE_JSON)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    schema = load_schema(args.schema)
    mdc, source_rows = parse_sheet(args.sheet, schema)
    write_json(mdc, args.out)
    roster = build_roster(mdc, args.sheet)
    write_json(roster, args.roster)
    unbound = []
    if os.path.exists(args.sheet):
        headers = _header_map(args.sheet, schema)
        unbound = resolve_bindings({_norm(h): c for c, h in headers.items()}, schema)["unbound"]
    print_coverage(mdc, unbound)
    print(f"\nWrote {args.out}")
    print(f"Wrote {args.roster} (roster: {roster['count']} repos)")
    if args.report:
        report = reconcile(mdc, source_rows, load_json(args.tags))
        write_json(report, args.report_json)
        write_text(markdown_report(report), args.report_md)
        print(f"Wrote {args.report_md}\nWrote {args.report_json}")
    return 0


def _header_map(path, schema):
    """Header row {column: text} — used only to surface unbound-field warnings in main()."""
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        root = ET.fromstring(archive.read(_sheet_path(archive, schema.get("sheet_name", ""))))
    rows = root.findall("main:sheetData/main:row", NS)
    return {c: h.strip() for c, h in _row_values(rows[0], strings).items() if h.strip()} if rows else {}


if __name__ == "__main__":
    raise SystemExit(main())
