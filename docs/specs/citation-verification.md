# Spec: citation verification guard

**Audience:** external Codex IMPLEMENTS; internal Codex VERIFIES against the real
mirror. Read `BACKLOG.md` "Project context" + "Guardrails" first.

## Goal

The product promise is "every claim is cited `repo/path:line`." Prove it: after
the model answers, check each cited file exists under `mirror/` and the line is in
range. Return a `citations` list on the result and mark fabricated ones in the UI.
**Do not rewrite the answer** — only annotate.

## Where

- New `retriever/citations.py` (pure, reusable — the eval harness will import it too).
- `webapp/agent.py` — attach `citations` to the returned result.
- `webapp/static/index.html` — mark pills verified/unverified + a summary chip.

## Design decisions (made — don't re-litigate)

- Extract citations from the final answer TEXT (works regardless of markdown).
- Resolve a `repo/path/File.java:line` first as a mirror-relative path; if that
  misses, fall back to a unique-basename match (`File.java` appearing exactly once).
  Ambiguous/zero matches → `ok=false`.
- Read-only. **Path-traversal guard:** the resolved real path MUST stay inside
  `mirror/`. Stdlib only. Touch only cited files (plus one cached filename index).

## Backend

### `retriever/citations.py`

```python
import os, re, functools
from . import config

_CITE = re.compile(
    r"([\w./\-]+?\.(?:java|xml|ya?ml|properties|kts?|json|sql))(?::(\d+)(?:-\d+)?)?",
    re.IGNORECASE,
)

def extract(text):
    """[(ref, path, line|None)] in order, de-duplicated."""
    out, seen = [], set()
    for m in _CITE.finditer(text or ""):
        ref = m.group(0)
        if ref in seen:
            continue
        seen.add(ref)
        out.append((ref, m.group(1), int(m.group(2)) if m.group(2) else None))
    return out

@functools.lru_cache(maxsize=1)
def _basename_index():
    idx = {}
    mirror_real = os.path.realpath(config.MIRROR)
    for dp, dn, fn in os.walk(mirror_real):
        dn[:] = [d for d in dn if d not in ('.git', 'target', 'build', 'node_modules', '.codegraph')]
        for name in fn:
            idx.setdefault(name, []).append(os.path.join(dp, name))
    return idx

def _inside_mirror(path):
    mirror_real = os.path.realpath(config.MIRROR)
    return os.path.realpath(path).startswith(mirror_real + os.sep)

def _resolve(path):
    cand = os.path.join(config.MIRROR, *path.split("/"))
    if os.path.isfile(cand) and _inside_mirror(cand):
        return cand
    matches = _basename_index().get(os.path.basename(path), [])
    return matches[0] if len(matches) == 1 else None

def verify(text):
    results = []
    for ref, path, line in extract(text):
        resolved = _resolve(path)
        if not resolved:
            results.append({"ref": ref, "ok": False, "reason": "not found in mirror"})
            continue
        if line is not None:
            try:
                with open(resolved, encoding="utf-8", errors="replace") as f:
                    n = sum(1 for _ in f)
            except OSError:
                n = 0
            if line > n:
                results.append({"ref": ref, "ok": False, "reason": f"line {line} > {n}"})
                continue
        results.append({"ref": ref, "ok": True, "reason": ""})
    ok = sum(1 for r in results if r["ok"])
    return {"items": results, "verified": ok, "total": len(results)}
```

### `webapp/agent.py`

In `answer()` (and, if `docs/specs/streaming.md` has landed, on the `done` event in
`answer_events()`), attach the report before returning:

```python
from . import citations
...
final = {"answer": text, "tool_trace": trace, "usage": usage}
final["citations"] = citations.verify(text)
return final
```

`server.py` already sends the whole `result`, so `citations` reaches the client
with no server change. `session_store` may optionally persist it (not required).

## Frontend (`webapp/static/index.html`)

After the answer is rendered (in the `ask`/stream `done` handler, right after
`setBubbleContent(bubble, answer, true)`), reconcile pills with the report:

```js
function markCitations(bubble, report) {
  if (!report) return;
  const status = {};
  (report.items || []).forEach((it) => { status[it.ref] = it; });
  bubble.querySelectorAll('code.cite').forEach((c) => {
    const it = status[(c.textContent || '').trim()];
    if (!it) return;
    c.classList.add(it.ok ? 'cite-ok' : 'cite-bad');
    if (!it.ok) c.title = 'Unverified: ' + it.reason;
  });
  // summary chip
  const wrap = bubble.querySelector('.evidence-section') || bubble;
  const chip = document.createElement('div');
  chip.className = 'cite-summary';
  chip.textContent = report.total
    ? `${report.verified}/${report.total} citations verified against source`
    : 'no citations to verify';
  wrap.appendChild(chip);
}
```
Call `markCitations(bubble, d.citations)` in the done/response handler. Add CSS:
`code.cite-bad` = red border/background + a ⚠; `code.cite-ok` keeps the normal
pill; `.cite-summary` = small muted line (green if all verified, amber if any bad).

## Keep working

- Answer text is unchanged; `citations` is additive. `LLM_MOCK` still returns
  (empty citations, since the mock answer has none). `/api/chat` shape gains one key.

## Done when

1. `retriever.citations.verify("... IngressResource.java:69 ...")` returns
   `ok=true` for a real ref and `ok=false` for `NoSuchFile.java:1` and for a real
   file with an out-of-range line.
2. A `..`/absolute path can never resolve outside `mirror/` (traversal guard).
3. UI shows "N/M citations verified"; fabricated pills render red with a reason.

## Verification steps (internal Codex, real mirror)

```bash
python -B -c "from retriever import citations; print(citations.verify('see IngressResource.java:69 and NoWayThisExists.java:9'))"
```
Expect one `ok:true`, one `ok:false`. Then ask a real question in the UI and
confirm the "verified" count and that any bad citation is flagged.

## Notes

- `_basename_index` is cached for the process; if the mirror is refreshed at
  runtime, clear it (`_basename_index.cache_clear()`).
- Keep it fast: only cited files are opened; the basename index is built once.
