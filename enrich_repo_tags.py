#!/usr/bin/env python3
"""Read MDC business metadata from its XLSX sheet and emit additive repo tags."""
import argparse
import json
import os
import posixpath
import zipfile
import xml.etree.ElementTree as ET

from retriever import config


SHEET_NAME = "full Repository List"
CHANNEL_COLUMNS = {
    "SMS": "sms", "EMAIL": "email", "PUSH": "push", "WhatsAPP": "whatsapp",
    "Letter": "letter", "Wechat": "wechat", "Others": "other",
}
REQUIRED_HEADERS = {
    "Repository", "MDC Common", *CHANNEL_COLUMNS,
    "Remark", "Batch/Realtime(B/R)", "Maraketing/Servicing(M/S)",
    "TimeCritcal(Y/N)", "CMB/WPB",
}
NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pkg": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _text(node):
    return "".join(node.itertext()) if node is not None else ""


def _column(cell_ref):
    return "".join(char for char in cell_ref if char.isalpha())


def _shared_strings(archive):
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [_text(item) for item in root.findall("main:si", NS)]


def _sheet_path(archive):
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in rels.findall("pkg:Relationship", NS)}
    for sheet in workbook.findall("main:sheets/main:sheet", NS):
        if sheet.get("name") == SHEET_NAME:
            target = targets.get(sheet.get("{%s}id" % NS["rel"]))
            if not target:
                break
            return posixpath.normpath(posixpath.join("xl", target.lstrip("/")))
    raise ValueError("XLSX is missing the 'full Repository List' sheet")


def _cell_value(cell, strings):
    kind = cell.get("t")
    if kind == "inlineStr":
        return _text(cell.find("main:is", NS)).strip()
    value = _text(cell.find("main:v", NS)).strip()
    if kind == "s" and value:
        return strings[int(value)].strip()
    return value


def _truthy(value):
    return str(value).strip().lower() in {"y", "yes", "true", "1", "x"}


def _row_values(row, strings):
    return {_column(cell.get("r", "")): _cell_value(cell, strings) for cell in row.findall("main:c", NS)}


def parse_sheet(path):
    """Return (metadata_by_normalized_repo, source_row_by_repo) using only stdlib."""
    with zipfile.ZipFile(path) as archive:
        strings = _shared_strings(archive)
        root = ET.fromstring(archive.read(_sheet_path(archive)))
    rows = root.findall("main:sheetData/main:row", NS)
    if not rows:
        return {}, {}
    header_values = _row_values(rows[0], strings)
    headers = {column: value.strip() for column, value in header_values.items()}
    if not REQUIRED_HEADERS.issubset(set(headers.values())):
        missing = sorted(REQUIRED_HEADERS - set(headers.values()))
        raise ValueError("MDC sheet headers do not match confirmed schema: " + ", ".join(missing))
    columns = {header: column for column, header in headers.items()}

    output, source_rows = {}, {}
    for row in rows[1:]:
        values = _row_values(row, strings)
        repo = values.get(columns["Repository"], "").strip().lower()
        if not repo:
            continue
        declared = [
            normalized for header, normalized in CHANNEL_COLUMNS.items()
            if _truthy(values.get(columns[header], ""))
        ]
        mode_raw = values.get(columns["Batch/Realtime(B/R)"], "").strip().upper()
        marketing_raw = values.get(columns["Maraketing/Servicing(M/S)"], "").strip().upper()
        business = values.get(columns["CMB/WPB"], "").strip().lower()
        output[repo] = {
            "mdc_common": _truthy(values.get(columns["MDC Common"], "")),
            "marketing_servicing": {"M": "marketing", "S": "servicing"}.get(marketing_raw, ""),
            "time_critical": _truthy(values.get(columns["TimeCritcal(Y/N)"], "")),
            "business_line": business if business in {"cmb", "wpb"} else "",
            "channel_declared": declared,
            "mode_declared": {"R": "realtime", "B": "batch"}.get(mode_raw, ""),
        }
        source_rows[repo] = int(row.get("r", "0") or 0)
    return output, source_rows


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


def print_coverage(payload):
    print("MDC repo metadata coverage\n")
    print(f"{'metric':28} {'count':>6}")
    print("-" * 35)
    for label, count in coverage_rows(payload):
        print(f"{label:28} {count:6d}")


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
                "citation": f"MDC_Repo_List_Analysis.xlsx:full Repository List row {source_rows.get(repo, 0)}",
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
                    f"MDC_Repo_List_Analysis.xlsx:full Repository List row {source_rows[repo]}"
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
    parser.add_argument("--out", default=config.REPO_TAGS_MDC_JSON)
    parser.add_argument("--report", action="store_true", help="write reconciliation Markdown and JSON")
    parser.add_argument("--tags", default=config.REPO_TAGS_JSON)
    parser.add_argument("--report-md", default=config.TAG_RECONCILE_MD)
    parser.add_argument("--report-json", default=config.TAG_RECONCILE_JSON)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    mdc, source_rows = parse_sheet(args.sheet)
    write_json(mdc, args.out)
    print_coverage(mdc)
    print(f"\nWrote {args.out}")
    if args.report:
        report = reconcile(mdc, source_rows, load_json(args.tags))
        write_json(report, args.report_json)
        write_text(markdown_report(report), args.report_md)
        print(f"Wrote {args.report_md}\nWrote {args.report_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
