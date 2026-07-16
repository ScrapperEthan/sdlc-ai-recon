#!/usr/bin/env python3
"""Measure how reliably the Q&A first answer pins the cross-repo caller with a file:line.

Fires the "who calls IngressService" question at the running webapp N times per phrasing and
tallies how often the FIRST answer surfaces the campaign-core caller — and how often it does so
WITH a line number. Use it to judge model-behaviour flakiness (e.g. right after a model swap like
gpt-5.5 -> gpt-5.6-terra) with an objective hit-rate, instead of eyeballing a single run.

Run with the webapp up (python -m webapp.server, ideally against the full SDLC_MIRROR):
    python probe_whocalls.py --n 5
Each run costs 2 * N model calls (two phrasings), so keep N small.
"""
import argparse
import json
import re
import urllib.request

# The cross-repo caller we expect a good "who calls IngressService" answer to surface, with a line.
CALLER_WITH_LINE = re.compile(r"SendCampaignEventService\.java:\d+", re.IGNORECASE)
CAMPAIGN_CALLER = re.compile(r"campaign-core", re.IGNORECASE)

PHRASINGS = {
    # the terse question RUNBOOK-30 used — ambiguous (who-calls vs what-is), the one that flaked
    "terse": "谁调用了 IngressService？",
    # an unambiguous phrasing that asks for the caller list + a line for each
    "explicit": ("谁调用了 IngressService？跨 repo 的调用链是什么？"
                 "列出真实的跨仓库调用者，并给出每个调用者的源码 file:line。"),
}


def ask(base, question, timeout):
    body = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + "/api/chat", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def score(result):
    answer = result.get("answer") or ""
    citations = (result.get("citations") or {}).get("items", []) or []
    blob = answer + "\n" + " ".join(item.get("ref", "") for item in citations)
    has_line = bool(CALLER_WITH_LINE.search(blob))
    has_caller = has_line or bool(CAMPAIGN_CALLER.search(blob))
    return has_caller, has_line


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    print(f"probing {args.base}  n={args.n} per phrasing\n")
    for label, question in PHRASINGS.items():
        caller_hits = line_hits = runs = 0
        for i in range(1, args.n + 1):
            try:
                result = ask(args.base, question, args.timeout)
            except Exception as error:  # noqa: BLE001
                print(f"[{label} {i}/{args.n}] ERROR: {error}")
                continue
            runs += 1
            has_caller, has_line = score(result)
            caller_hits += has_caller
            line_hits += has_line
            print(f"[{label} {i}/{args.n}] campaign-core caller: {'Y' if has_caller else 'N'}"
                  f" | with :line: {'Y' if has_line else 'N'}")
        print(f"== {label}: caller {caller_hits}/{runs}, with :line {line_hits}/{runs}\n")


if __name__ == "__main__":
    main()
