"""Answer-quality regression for the assistant.

The 1000+ unit tests check plumbing: return shapes, ownership scoping, redaction. NONE of them
check whether an ANSWER is any good, so every prompt/tool-description/model change has been a
guess. This is the missing half.

**Why string assertions are enough here.** Grading prose would need a judge model and would itself
drift. But every defect this project has actually shipped has ONE shape: *it did not look, and said
something that reads as if it had.* A missing timezone refused the plan and the tool was called
anyway (RUNBOOK-61); a tool's own error body became "log evidence" (2026-07-30); a use-case count of
zero read as "no business impact". All of those are detectable with `must_not_mention` plus the tool
trace — no judgement required, no second model in the loop.

So the checks are deliberately blunt and mostly NEGATIVE:

  must_not_mention          the sentence that would be a lie if the lookup did not happen
  must_ask_back             it stopped and asked instead of guessing
  must_not_call_tools       it did not send the request it just refused to send
  citations_must_verify     every repo/path:line resolves in the mirror (retriever/citations.py)
  must_mention_any          at least one of the caveat words that make the claim honest

Every case carries a `why` naming the RUNBOOK or owner decision it encodes. A case with no `why`
is a case nobody will trust when it goes red.

Running it:

    python -m evals.run                     # in-process, needs mirror + a real model
    python -m evals.run --http http://127.0.0.1:8765/api/chat
    python -m evals.run --lane incident     # just one lane
    python -m evals.run --case honesty-*    # one or a few, by id glob

Costs one real model turn per case (some run several tool iterations), so this is a
before/after-a-change gate, not a per-commit hook. `--out` keeps the last run so the table can
show a per-case delta; that file is gitignored, and the box is where the real numbers come from.
"""
import argparse
import fnmatch
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request

try:
    from retriever import citations
except Exception:  # noqa: BLE001 -- the runner must still work without the retrieval layer
    citations = None


DEFAULT_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.jsonl")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.json")
DEFAULT_REPOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "config", "eval_repos.json")


def load_repos(path=DEFAULT_REPOS):
    """`{placeholder: real id}` from the intranet-owned config, minus the `_README` block."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {key: str(value).strip() for key, value in data.items()
            if not key.startswith("_") and str(value).strip()}


def resolve_case(case, repos):
    """Substitute `{placeholder}` in the question. Returns (case, missing_placeholders).

    A case with an unresolved placeholder is SKIPPED, never run. RUNBOOK-65: the external side wrote
    a repo id that does not exist into a runbook and into these cases; the system fail-closed
    correctly, but a case built on a repo nobody has heard of measures nothing, and a BASELINE built
    on one is worse than having no baseline at all. Names only the box can observe belong to the box.
    """
    question = case.get("question") or ""
    missing = sorted({name for name in re.findall(r"\{([a-z_]+)\}", question)
                      if not repos.get(name)})
    if missing:
        return case, missing
    resolved = dict(case)
    for name, value in repos.items():
        question = question.replace("{" + name + "}", value)
    resolved["question"] = question
    return resolved, []

# --- did it CLAIM this, or did it quote it in order to deny it? --------------------------------
# RUNBOOK-66 baseline: five of six reds were this checker's fault. A substring test cannot tell
#
#   "未发现异常"                                   <- the lie we are hunting
#   '这不是"日志正常"或"没有异常"的结论'            <- the IDEAL answer
#
# apart, and the model was writing the second. Quoting the forbidden phrase in order to rule it out
# is better behaviour than avoiding the words, so a checker that punishes it would train exactly the
# wrong thing — it would push the model to stop naming the misreading it is trying to prevent.
#
# Two signals, both taken from the real answers that failed: the phrase sits inside quotation marks,
# or a negator immediately precedes it. Every occurrence must be negated for the case to pass — one
# bare assertion anywhere is still a fail.
_QUOTE_PAIRS = (("“", "”"), ("‘", "’"), ("「", "」"),
                ("『", "』"), ('"', '"'), ("'", "'"), ("`", "`"))
# Compounds only. A bare "不" is too loose — it matches "不过" ("however"), which would let
# "不过总体看，未发现异常" launder a real claim.
_NEGATORS = ("不是", "不能", "不可", "不应", "不会", "不代表", "不等于", "不意味", "不构成",
             "不表示", "不说明", "不属于", "不算", "不得", "不该", "不叫",
             "并非", "而非", "而不是", "无法", "没有理由",
             "not ", "never ", "cannot ", "isn't", "is not", "does not", "doesn't")
# How far back to look for a negator. Chinese negation sits close to what it negates; a wide window
# would start finding unrelated 不 from the previous clause.
_NEGATION_WINDOW = 14


def _occurrence_is_negated(text, start, end):
    """True when this occurrence is quoted or directly negated, i.e. mentioned rather than claimed."""
    for open_mark, close_mark in _QUOTE_PAIRS:
        before, after = text[:start], text[end:]
        # Immediately wrapped: '...「没有异常」...' or '..."no anomaly"...'
        if before.endswith(open_mark) and after.startswith(close_mark):
            return True
    window = text[max(0, start - _NEGATION_WINDOW):start]
    lowered = window.lower()
    return any(negator in window or negator in lowered for negator in _NEGATORS)


def asserts_phrase(text, phrase):
    """True if `text` states `phrase` as its own claim, rather than quoting or denying it."""
    haystack, needle = (text or "").lower(), (phrase or "").lower()
    if not needle:
        return False
    index = haystack.find(needle)
    while index >= 0:
        if not _occurrence_is_negated(text, index, index + len(needle)):
            return True                      # one bare assertion is enough to fail the case
        index = haystack.find(needle, index + 1)
    return False


# Phrases that count as "it stopped and asked" rather than "it answered anyway". Deliberately
# generous: the check that carries the weight is `must_not_mention`, and a false PASS here is
# always accompanied by a real failure there.
_ASK_BACK_MARKERS = ("?", "？", "请提供", "请告诉", "请确认", "需要你", "无法确定", "不能确定",
                     "哪一天", "哪天", "哪个", "which", "please provide", "cannot determine",
                     "partial", "unverified", "拒绝", "无法")


def _load_cases(path):
    with open(path, encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _answer_in_process(question):
    from webapp.agent import answer

    return answer(question)


def _answer_http(question, endpoint):
    payload = json.dumps({"question": question}).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body[:300]}") from error


def _verified_citations(text, result):
    """(verified, unverified, refs). Prefers the report the agent already produced."""
    report = result.get("citations")
    if not isinstance(report, dict) or not isinstance(report.get("items"), list):
        if citations is None:
            return 0, 0, []
        report = citations.verify(text or "")
    items = report.get("items") or []
    verified = [item for item in items if item.get("ok")]
    unverified = [item for item in items if not item.get("ok")]
    return len(verified), len(unverified), [item.get("ref", "") for item in verified]


def _evaluate(case, result):
    """Run every declared assertion. Returns a list of {check, ok, detail}."""
    text = result.get("answer") or ""
    lowered = text.lower()
    tools = [item.get("tool") for item in result.get("tool_trace") or []]
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    for phrase in case.get("must_mention", []):
        check(f"say:{phrase}", phrase in text)

    wanted_any = case.get("must_mention_any") or []
    if wanted_any:
        hit = [p for p in wanted_any if p.lower() in lowered]
        check("say-any", bool(hit), "none of: " + " | ".join(wanted_any) if not hit else hit[0])

    # The load-bearing one: the sentence that would be a lie if the lookup never happened. Judged on
    # whether it was CLAIMED — quoting it in order to deny it is the answer we want, not a failure.
    for phrase in case.get("must_not_mention", []):
        claimed = asserts_phrase(text, phrase)
        check(f"never:{phrase}", not claimed, "CLAIMED IT" if claimed else "")

    if case.get("must_ask_back"):
        asked = any(marker in text or marker in lowered for marker in _ASK_BACK_MARKERS)
        check("ask-back", asked, "answered instead of asking" if not asked else "")

    for tool in case.get("must_call_tools", []):
        check(f"tool:{tool}", tool in tools, f"called {tools}" if tool not in tools else "")

    any_tools = case.get("must_call_tools_any") or []
    if any_tools:
        hit = [t for t in any_tools if t in tools]
        check("tool-any", bool(hit), f"called {tools}" if not hit else hit[0])

    for tool in case.get("must_not_call_tools", []):
        called = tool in tools
        check(f"no-tool:{tool}", not called, "CALLED IT" if called else "")

    if "max_tool_calls" in case:
        check("tool-budget", len(tools) <= case["max_tool_calls"], f"{len(tools)} calls")

    if case.get("citations_must_verify") or "max_unverified_citations" in case \
            or "min_verified_citations" in case or case.get("citations_need_line_numbers"):
        verified, unverified, refs = _verified_citations(text, result)
        if case.get("citations_must_verify"):
            check("cite-verify", unverified == 0, f"{unverified} unverified" if unverified else "")
        if "max_unverified_citations" in case:
            check("cite-max-bad", unverified <= case["max_unverified_citations"],
                  f"{unverified} unverified")
        if "min_verified_citations" in case:
            check("cite-min", verified >= case["min_verified_citations"],
                  f"only {verified} verified")
        if case.get("citations_need_line_numbers"):
            with_lines = [ref for ref in refs if ":" in ref and ref.rsplit(":", 1)[-1].isdigit()]
            check("cite-lines", bool(with_lines) and len(with_lines) == len(refs),
                  f"{len(refs) - len(with_lines)} file-only")

    for glob in case.get("must_cite_globs", []):
        _v, _u, refs = _verified_citations(text, result)
        check(f"cite-glob:{glob}", any(fnmatch.fnmatch(ref, glob) for ref in refs))

    if "must_flag_partial" in case:
        flagged = "partial" in lowered or "unverified" in lowered or "未验证" in text
        check("partial", flagged == case["must_flag_partial"])

    return checks


def _run_case(case, http_endpoint=None):
    started = time.time()
    try:
        result = (_answer_http(case["question"], http_endpoint) if http_endpoint
                  else _answer_in_process(case["question"]))
    except Exception as error:  # noqa: BLE001 -- one broken case must not lose the whole run
        return {"id": case["id"], "lane": case.get("lane", ""), "error": str(error)[:200],
                "checks": [{"check": "ran", "ok": False, "detail": str(error)[:120]}],
                "passed": 0, "total": 1, "seconds": round(time.time() - started, 1),
                "tools": [], "answer": "", "traceback": traceback.format_exc()[-800:]}

    checks = _evaluate(case, result)
    return {
        "id": case["id"],
        "lane": case.get("lane", ""),
        "checks": checks,
        "passed": sum(1 for item in checks if item["ok"]),
        "total": len(checks),
        "seconds": round(time.time() - started, 1),
        "tools": [item.get("tool") for item in result.get("tool_trace") or []],
        # Kept so a red case can be diagnosed from the saved run without re-spending a model call.
        "answer": (result.get("answer") or "")[:4000],
    }


def _previous(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {item["id"]: item for item in data.get("results", [])}


def _delta(result, previous):
    """'=' / '↓ was PASS' / '↑ now PASS' / 'new'. The column the whole exercise is for."""
    before = previous.get(result["id"])
    if before is None:
        return "new"
    was_green = before.get("passed") == before.get("total") and before.get("total")
    now_green = result["passed"] == result["total"] and result["total"]
    if was_green and not now_green:
        return "DOWN was PASS"
    if now_green and not was_green:
        return "up now PASS"
    if before.get("passed") != result["passed"]:
        return f"{before.get('passed')}->{result['passed']}"
    return "="


def _print_table(results, previous):
    """Narrow on purpose: the box runs this and the result comes back as a PHONE PHOTO."""
    width = min(46, max((len(r["id"]) for r in results), default=20))
    print()
    print(f"{'CASE'.ljust(width)}  {'RESULT':<6}  {'CHK':<7}  {'SEC':<5}  VS LAST")
    print("-" * (width + 34))
    for result in results:
        verdict = "PASS" if result["total"] and result["passed"] == result["total"] else "FAIL"
        name = result["id"][:width].ljust(width)
        print(f"{name}  {verdict:<6}  {str(result['passed']) + '/' + str(result['total']):<7}  "
              f"{str(result['seconds']):<5}  {_delta(result, previous)}")
    print("-" * (width + 34))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Assistant answer-quality regression.")
    parser.add_argument("--cases", default=DEFAULT_CASES)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--repos", default=DEFAULT_REPOS,
                        help="intranet-owned map of {placeholder} -> real repo/use-case id")
    parser.add_argument("--http", help="POST to a running /api/chat instead of calling in-process")
    parser.add_argument("--lane", help="only cases with this lane (incident / retrieval)")
    parser.add_argument("--case", help="only case ids matching this glob")
    parser.add_argument("--quiet", action="store_true", help="table only, no per-failure detail")
    args = parser.parse_args(argv)

    cases = _load_cases(args.cases)
    if args.lane:
        cases = [c for c in cases if c.get("lane") == args.lane]
    if args.case:
        cases = [c for c in cases if fnmatch.fnmatch(c["id"], args.case)]
    if not cases:
        print("no cases selected")
        return 2

    repos = load_repos(args.repos)
    previous = _previous(args.out)
    results, skipped = [], []
    for index, case in enumerate(cases, 1):
        resolved, missing = resolve_case(case, repos)
        if missing:
            skipped.append((case["id"], missing))
            print(f"[{index}/{len(cases)}] {case['id']} SKIPPED (no real id for "
                  f"{', '.join(missing)})", flush=True)
            continue
        print(f"[{index}/{len(cases)}] {case['id']} ...", flush=True)
        results.append(_run_case(resolved, args.http))

    if skipped:
        needed = sorted({name for _id, names in skipped for name in names})
        print(f"\n!! {len(skipped)} case(s) SKIPPED — fill these in {args.repos}: "
              f"{', '.join(needed)}")
        print("!! They are not failures and not passes. A case run against a made-up repo id")
        print("!! measures nothing, so it is not run at all (RUNBOOK-65).")
        for case_id, names in skipped:
            print(f"     {case_id}  needs {', '.join(names)}")

    if not results:
        print("\nnothing ran.")
        return 2

    _print_table(results, previous)

    green = sum(1 for r in results if r["total"] and r["passed"] == r["total"])
    passed = sum(r["passed"] for r in results)
    total = sum(r["total"] for r in results)
    before_green = sum(1 for r in results
                       if (previous.get(r["id"]) or {}).get("passed")
                       == (previous.get(r["id"]) or {}).get("total")
                       and (previous.get(r["id"]) or {}).get("total"))
    print(f"{green}/{len(results)} cases PASS      {passed}/{total} checks"
          + (f"      (last run: {before_green}/{len(previous)} cases)" if previous else ""))

    if not args.quiet:
        for result in results:
            failed = [c for c in result["checks"] if not c["ok"]]
            if not failed:
                continue
            print(f"\n--- {result['id']}  tools={result['tools']}")
            for item in failed:
                print(f"    FAIL {item['check']}" + (f"   ({item['detail']})" if item["detail"] else ""))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "cases_green": green, "cases_total": len(results),
                   "checks_passed": passed, "checks_total": total,
                   "results": results}, handle, ensure_ascii=False, indent=2)
    print(f"\nsaved -> {args.out}")
    return 0 if green == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
