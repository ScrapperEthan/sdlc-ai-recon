#!/usr/bin/env python3
"""Dump the raw CodeGraph caller block the Q&A uses for a symbol, so we can see its format.

    python dump_callgraph.py IngressService

Run from the repo root with SDLC_MIRROR set (so a bare symbol routes to the bundle that defines
it). Prints the routing header + the first N lines of the raw `codegraph explore <symbol>` output,
and saves the full callers block to index/reports/CG_<symbol>.json for the record. Read-only.
"""
import argparse
import json
import os

from retriever import unified_impact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("--head", type=int, default=45)
    args = parser.parse_args()

    result = unified_impact.query(args.symbol)
    callers = result.get("callers") or {}

    os.makedirs(os.path.join("index", "reports"), exist_ok=True)
    out_path = os.path.join("index", "reports", f"CG_{args.symbol}.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(callers, handle, ensure_ascii=False, indent=2)

    output = callers.get("output") or ""
    print(f"symbol={args.symbol}")
    print(f"available={callers.get('available')} returncode={callers.get('returncode')} "
          f"bundle_root={callers.get('bundle_root')}")
    print(f"output_len={len(output)}  fallback_hits={len(callers.get('fallback_hits') or [])}")
    print(f"(full callers block saved to {out_path})")
    print(f"----- first {args.head} lines of raw codegraph output -----")
    print("\n".join(output.splitlines()[: args.head]))
    if not output and callers.get("fallback_hits"):
        print(f"----- no codegraph output; first {args.head} fallback lexical hits -----")
        for hit in (callers.get("fallback_hits") or [])[: args.head]:
            print(hit)


if __name__ == "__main__":
    main()
