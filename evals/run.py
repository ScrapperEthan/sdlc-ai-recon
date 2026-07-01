"""Run soft-scored assistant answer-quality checks."""
import argparse
import fnmatch
import json
import os
import time
import urllib.error
import urllib.request

try:
    from retriever import citations
except Exception:  # noqa: BLE001
    citations = None

if citations is None:
    import re

    _CITE = re.compile(
        r"[\w./\-]+?\.(?:java|xml|ya?ml|properties|kts?|json|sql)(?::\d+(?:-\d+)?)?",
        re.IGNORECASE,
    )


def _load(path):
    with open(path, encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _extract_citations(text):
    if citations is not None:
        return [item[0] for item in citations.extract(text)]
    return _CITE.findall(text or "")


def _answer_in_process(question):
    from webapp.agent import answer

    return answer(question)


def _answer_http(question, endpoint):
    payload = json.dumps({"question": question}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body}") from error


def _run_case(case, http_endpoint=None):
    result = (
        _answer_http(case["question"], http_endpoint)
        if http_endpoint
        else _answer_in_process(case["question"])
    )
    text = result.get("answer") or ""
    tools = [item.get("tool") for item in result.get("tool_trace") or []]
    cited_refs = _extract_citations(text)
    checks = []

    def check(name, ok):
        checks.append({"check": name, "ok": bool(ok)})

    for tool in case.get("must_call_tools", []):
        check(f"tool:{tool}", tool in tools)
    for repo in case.get("must_mention_repos", []):
        check(f"repo:{repo}", repo in text)
    for glob in case.get("must_cite_globs", []):
        check(f"cite:{glob}", any(fnmatch.fnmatch(ref, glob) for ref in cited_refs))
    if "must_flag_partial" in case:
        flagged = "partial" in text.lower() or "unverified" in text.lower()
        check("partial", flagged == case["must_flag_partial"])

    passed = sum(1 for item in checks if item["ok"])
    return {
        "id": case["id"],
        "score": passed,
        "total": len(checks),
        "checks": checks,
        "tools": tools,
        "answer_chars": len(text),
    }


def _previous_scores(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return {result["id"]: result["score"] for result in data.get("results", [])}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run assistant answer-quality evals.")
    parser.add_argument("--cases", default="evals/cases.jsonl", help="JSONL case file")
    parser.add_argument("--out", default="evals/last_run.json", help="last-run JSON path")
    parser.add_argument("--http", help="POST /api/chat endpoint instead of in-process answer()")
    args = parser.parse_args(argv)

    cases = _load(args.cases)
    previous = _previous_scores(args.out)
    results = [_run_case(case, args.http) for case in cases]

    passed = sum(result["score"] for result in results)
    total = sum(result["total"] for result in results)
    print(f"\n=== eval: {passed}/{total} checks passed across {len(results)} cases ===")
    for result in results:
        print(f"  [{result['score']}/{result['total']}] {result['id']}  tools={result['tools']}")
        for check in result["checks"]:
            if not check["ok"]:
                print(f"        FAIL {check['check']}")

    for result in results:
        old = previous.get(result["id"])
        if old is not None and old != result["score"]:
            print(f"  delta {result['id']}: {old} -> {result['score']}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(
            {"ts": time.time(), "passed": passed, "total": total, "results": results},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
