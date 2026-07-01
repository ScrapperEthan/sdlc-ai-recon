# Spec: eval harness (answer-quality regression)

**Audience:** external Codex IMPLEMENTS; internal Codex RUNS it against the real
copilot-api. Read `BACKLOG.md` "Project context" + "Guardrails" first.

## Goal

Replace eyeballing `index/qa-eval.md` with a repeatable, scored check: given a set
of questions with expectations, run the assistant and score whether it used the
right tools, named the right repos, cited matching files, and correctly flagged
partial. So prompt/tool/model changes can be compared objectively.

## Where

- New `evals/cases.jsonl` (dataset), `evals/run.py` (runner), `evals/__init__.py`.
- Imports `webapp.agent.answer` (in-process) OR calls `POST /api/chat` (choose via
  a flag). Reuses `retriever.citations.extract` if `citation-verification.md` landed;
  otherwise inline the same regex.

## Design decisions (made)

- **Dataset = JSONL**, one case per line, easy to append. Expectations are soft
  string/glob matches, scored independently (partial credit), not exact-match.
- **Needs the real model** to be meaningful (mock returns canned text — a mock run
  only tests the plumbing and will fail content assertions; that's expected).
- Stdlib only. Deterministic scoring. Writes `evals/last_run.json` and prints a
  diff vs the previous run.

## Dataset — `evals/cases.jsonl`

Each line:
```json
{"id":"ingress-to-job","question":"How does a message get from ingress-api to a tracking job?",
 "must_call_tools":["search_code"],"must_mention_repos":["mc-hk-hase-api-ingress-core"],
 "must_cite_globs":["**/EventProducerService.java","**/*Listener.java"],"must_flag_partial":true}
{"id":"eventpayload-impact","question":"If I change EventPayload, what breaks?",
 "must_call_tools":["impact"],"must_mention_repos":[],"must_cite_globs":[],"must_flag_partial":false}
{"id":"otx-consumers","question":"Which repos consume otx_bat_letter?",
 "must_call_tools":["consumers"],"must_mention_repos":["mc-hk-hase-svc-bat-tracking-job"],
 "must_cite_globs":["**/*Listener.java"],"must_flag_partial":false}
```
Seed from the pilot questions in `RUNBOOK-2` / `RUNBOOK-3`. All fields optional
except `id` and `question` (empty expectation = not scored).

## Runner — `evals/run.py`

```python
import json, os, sys, fnmatch, time

def _load(path="evals/cases.jsonl"):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

CITE = __import__("re").compile(r"[\w./\-]+?\.(?:java|xml|ya?ml|properties|kts?|json|sql)(?::\d+(?:-\d+)?)?", 2)

def _run_case(case):
    from webapp.agent import answer            # in-process; real provider via env
    res = answer(case["question"])
    text = (res.get("answer") or "")
    tools = [t.get("tool") for t in res.get("tool_trace") or []]
    cites = CITE.findall(text)
    checks = []
    def check(name, ok): checks.append({"check": name, "ok": bool(ok)})

    for t in case.get("must_call_tools", []):
        check(f"tool:{t}", t in tools)
    for r in case.get("must_mention_repos", []):
        check(f"repo:{r}", r in text)
    for g in case.get("must_cite_globs", []):
        check(f"cite:{g}", any(fnmatch.fnmatch(c, g) for c in cites))
    if "must_flag_partial" in case:
        flagged = ("partial" in text.lower()) or ("unverified" in text.lower())
        check("partial", flagged == case["must_flag_partial"])

    passed = sum(c["ok"] for c in checks)
    return {"id": case["id"], "score": passed, "total": len(checks),
            "checks": checks, "tools": tools, "answer_chars": len(text)}

def main():
    cases = _load()
    results = [_run_case(c) for c in cases]
    p = sum(r["score"] for r in results); t = sum(r["total"] for r in results)
    print(f"\n=== eval: {p}/{t} checks passed across {len(results)} cases ===")
    for r in results:
        print(f"  [{r['score']}/{r['total']}] {r['id']}  tools={r['tools']}")
        for c in r["checks"]:
            if not c["ok"]:
                print(f"        FAIL {c['check']}")
    # diff vs previous
    prev_path = "evals/last_run.json"
    prev = {}
    if os.path.exists(prev_path):
        prev = {r["id"]: r["score"] for r in json.load(open(prev_path, encoding="utf-8"))["results"]}
    for r in results:
        old = prev.get(r["id"])
        if old is not None and old != r["score"]:
            print(f"  Δ {r['id']}: {old} -> {r['score']}")
    json.dump({"ts": time.time(), "passed": p, "total": t, "results": results},
              open(prev_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    sys.exit(0 if p == t else 1)

if __name__ == "__main__":
    main()
```

Run from the workspace root so `webapp`/`retriever` import and the data folders
resolve. Real model via the usual env (`LLM_PROVIDER`, `LLM_BASE_URL`, ...).

## Keep working

- Additive; touches nothing in `webapp/` or `retriever/` except (optionally)
  importing `retriever.citations`. `.gitignore` `evals/last_run.json`.

## Done when

1. `python -m evals.run` prints a per-case pass/fail table + an aggregate and
   exits non-zero if any check fails.
2. Editing an expectation or adding a case line changes the score as expected.
3. A second run after a prompt change shows the `Δ` diff lines.

## Verification steps (internal Codex, real copilot-api)

```bash
LLM_PROVIDER=copilot_responses LLM_BASE_URL=... python -m evals.run
```
Review the table; a low score points at a prompt/tool gap, not a harness bug.
(A `LLM_MOCK=1 python -m evals.run` run should execute end-to-end but fail content
checks — that only proves the plumbing.)

## Notes

- Keep expectations SOFT (substring / glob), not exact answer text — the model
  phrases things differently each run; we score behaviour, not wording.
- Grow the dataset whenever a real answer is wrong: add a case that would have
  caught it. That's how the harness earns its keep.
