#!/usr/bin/env python3
"""Resolve symbolic message destinations and report message-map coverage."""
import argparse
import csv
import os
import re

from retriever import config

_SKIP = {".git", "target", "build", "node_modules", ".codegraph"}
_ENUM_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*\(\s*\"([^\"]+)\"", re.MULTILINE)


def _iter_java_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP]
        for name in filenames:
            if name.endswith(".java"):
                yield os.path.join(dirpath, name)


def load_topic_symbols(mirror):
    """Return symbol -> {literal, evidence} for unique enum topic constants."""
    exact = {}
    bare = {}
    for path in _iter_java_files(mirror):
        stem = os.path.splitext(os.path.basename(path))[0]
        if "topic" not in stem.lower() and "router" not in stem.lower():
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        rel = os.path.relpath(path, mirror).replace(os.sep, "/")
        for const, literal in _ENUM_RE.findall(text):
            item = {"literal": literal, "evidence": f"{rel}:{const}"}
            exact[f"{stem}.{const}"] = item
            bare.setdefault(const, []).append(item)

    for const, matches in bare.items():
        literals = {match["literal"] for match in matches}
        if len(literals) == 1:
            exact[const] = matches[0]
    return exact


def _read_edges(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames or [], list(reader)


def _write_edges(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _coverage(rows):
    total = len(rows)
    producers = sum(1 for row in rows if (row.get("producer_repo") or "").strip())
    consumers = sum(1 for row in rows if (row.get("consumer_repo") or "").strip())
    partial = sum(
        1
        for row in rows
        if "partial" in (row.get("routing_source") or "").lower()
        or "unknown" in (row.get("routing_source") or "").lower()
        or not (row.get("producer_repo") or "").strip()
    )
    return {"total": total, "producers": producers, "consumers": consumers, "partial": partial}


def _percent(part, total):
    return 0.0 if not total else (part / total) * 100


def enrich(edges_path, mirror, dry_run=False):
    if not os.path.exists(edges_path):
        return {"available": False, "error": f"missing {edges_path}"}
    if not os.path.isdir(mirror):
        return {"available": False, "error": f"missing mirror {mirror}"}

    fieldnames, rows = _read_edges(edges_path)
    symbols = load_topic_symbols(mirror)
    before = _coverage(rows)
    changes = []

    for index, row in enumerate(rows, 1):
        raw = (row.get("destination") or "").strip()
        symbol = raw.split(".")[-1] if raw else ""
        match = symbols.get(raw) or symbols.get(symbol)
        if not match or match["literal"] == raw:
            continue
        row["destination"] = match["literal"]
        source = (row.get("routing_source") or "").strip()
        note = f"enum:{raw or symbol}->{match['literal']} ({match['evidence']})"
        row["routing_source"] = f"{source}; {note}" if source else note
        changes.append({"row": index, "from": raw, "to": match["literal"], "evidence": match["evidence"]})

    after = _coverage(rows)
    if changes and not dry_run:
        _write_edges(edges_path, fieldnames, rows)

    return {
        "available": True,
        "dry_run": dry_run,
        "symbols": len(symbols),
        "changes": changes,
        "before": before,
        "after": after,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enrich index/message_edges.csv from mirror topic enums.")
    parser.add_argument("--edges", default=config.MESSAGE_EDGES_CSV)
    parser.add_argument("--mirror", default=config.MIRROR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    report = enrich(args.edges, args.mirror, args.dry_run)
    if not report.get("available"):
        print(report["error"])
        return 1

    before = report["before"]
    after = report["after"]
    print(f"topic symbols: {report['symbols']}")
    print(f"resolved destinations: {len(report['changes'])}")
    print(
        "producer coverage: "
        f"{after['producers']}/{after['total']} "
        f"({100 - _percent(after['producers'], after['total']):.1f}% missing)"
    )
    print(
        "partial ratio: "
        f"{after['partial']}/{after['total']} "
        f"({_percent(after['partial'], after['total']):.1f}%)"
    )
    if before != after:
        print(f"coverage delta: {before} -> {after}")
    for change in report["changes"][:20]:
        print(f"  row {change['row']}: {change['from']} -> {change['to']}  [{change['evidence']}]")
    if len(report["changes"]) > 20:
        print(f"  ... {len(report['changes']) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
