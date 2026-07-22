# Spec: CodeGraph trust signals (payload caveat + routing ambiguity + freshness)

**Audience:** external Codex IMPLEMENTS; internal Codex VERIFIES against the real
mirror. Read `BACKLOG.md` "Project context" + "Guardrails" first.

## Goal

The call-graph tools (`unified_impact`, `call_graph`) already degrade honestly
(`callers.available` / `fallback_hits`) and the system prompt tells the model to
verify every edge in source. **But that discipline lives in
`prompts/qa-system-prompt.md` — a bare MCP agent calling `unified_impact` without
our prompt never sees it, and the payload itself carries no "don't over-trust this"
signal.** This closes that gap by moving three trust signals from prompt-side into
the tool RESULT, so *any* consumer gets them:

1. **Caveat (Gap A — the important one).** Every call-graph result carries a short
   static `caveat`: these are candidate edges from a static index; reflection /
   Spring AOP / proxies / config-driven dispatch may be missing; async flows aren't
   here; confirm in source before asserting.
2. **Routing ambiguity (Gap B).** When a bare symbol seed is defined by a
   `<Symbol>.java` file in **more than one** repo, bare-symbol routing silently
   picks one. Surface `routing_ambiguity` naming all candidate repos + the chosen
   one, so a same-name collision (the "IngressService matched 37 files" case) can't
   pass as a confident answer.
3. **Freshness (Gap C).** Stamp *when the CodeGraph index serving this query was
   built* into the `callers` block, plus whether the derived indexes were refreshed
   after it (a cheap "mirror may have moved on" hint).

**Additive only.** Do NOT change routing behaviour, the `available` /
`returncode` / `bundle_root` / `fallback_hits` fields, or `agent.answer()`'s
contract. No frontend change required.

## Where

- `retriever/unified_impact.py` — all changes live here.
- `tests/test_unified_impact_routing.py` — extend; keep existing assertions green.

## Design decisions (made — don't re-litigate)

- **Caveat is a module-level constant string**, stamped into *every* return branch
  of `_call_graph` (success, codegraph-missing, and exception) — so `call_graph`
  (raw) inherits it too, not just `unified_impact`.
- **Ambiguity is reported, routing is unchanged.** First-defining-repo still wins
  (back-compat); we only ADD a `routing_ambiguity` block when >1 repo defines the
  symbol. A repo seed has no `<repo>.java`, so it never triggers — conservative by
  construction, same guard the existing `_defining_repo` relies on.
- **Freshness uses real mtimes, never a guess.** `index_built_at` = mtime of the
  routed bundle's `.codegraph/codegraph.db` (the manifest has no reliable per-bundle
  timestamp). `indexes_refreshed_at` = `generated_at` from `index/last_indexed.json`.
  Both `null` when unavailable; `possibly_stale` is only ever `true` when both are
  known and the derived indexes are newer. Format timestamps to match `refresh.py`'s
  `_now()` (ISO-8601, `Z`, no microseconds) so a plain string `>` compares correctly.
- **`possibly_stale` is deliberately conservative** (it can fire after any index
  refresh even if the `.java` source didn't change). That is the safe failure mode
  here: the whole product thesis is "verify against source," so a hint that nudges
  toward double-checking is acceptable, and walking all ~390 repos to compute an
  exact answer violates the "keep it fast" rule. Document the meaning precisely so
  it reads as "double-check," not "definitely wrong."
- Stdlib only. Read-only. No new files, no new deps.

## Backend — `retriever/unified_impact.py`

### 1. New import + constant + helpers (near the top, after existing imports)

```python
from datetime import datetime, timezone
```

```python
# Stamped into every call-graph result so an agent using the MCP tool WITHOUT the qa-system-prompt
# still learns not to treat these edges as the whole truth. Short and machine-quotable on purpose.
CALL_GRAPH_CAVEAT = (
    "Candidate call edges from a STATIC index. Reflection, Spring AOP/proxies, runtime DI, and "
    "config/DB-driven dispatch may be MISSING or wrong. Async (MQ/topic) flows are NOT here — see "
    "message_edges. Treat each edge as a lead: confirm it in source (search_code/read_file) and "
    "cite repo/path/File.java:line before asserting it."
)


def _iso(epoch):
    """UTC ISO-8601 (…Z, no microseconds) — same shape as refresh.py `_now()`, so two of these
    string-compare correctly."""
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _index_freshness(cwd):
    """(index_built_at, indexes_refreshed_at, possibly_stale) for the CodeGraph index at `cwd`.

    index_built_at    — mtime of `<cwd>/.codegraph/codegraph.db` (the DB actually queried), or None.
    indexes_refreshed_at — `generated_at` from index/last_indexed.json (when the derived indexes
                           were last rebuilt), or None.
    possibly_stale    — True ONLY when both are known and the indexes were refreshed AFTER this DB
                        was built (the code mirror may have moved on since — double-check). Never
                        guessed: unknown timestamps => False.
    """
    built_at = None
    if cwd:
        try:
            built_at = _iso(os.path.getmtime(os.path.join(cwd, ".codegraph", "codegraph.db")))
        except OSError:
            built_at = None
    refreshed_at = None
    try:
        with open(os.path.join(config.INDEX_DIR, "last_indexed.json"), encoding="utf-8-sig") as handle:
            refreshed_at = (json.load(handle) or {}).get("generated_at")
    except (OSError, ValueError):
        refreshed_at = None
    stale = bool(built_at and refreshed_at and refreshed_at > built_at)
    return built_at, refreshed_at, stale
```

### 2. Stamp trust fields into `_call_graph` (all three return branches)

Compute the trust block once at the top of `_call_graph`, then spread it into each
return. The existing keys stay byte-for-byte; we only add `caveat`,
`index_built_at`, `indexes_refreshed_at`, `possibly_stale`.

```python
def _call_graph(seed, cwd=None):
    built_at, refreshed_at, stale = _index_freshness(cwd)
    trust = {
        "caveat": CALL_GRAPH_CAVEAT,
        "index_built_at": built_at,
        "indexes_refreshed_at": refreshed_at,
        "possibly_stale": stale,
    }
    cg = shutil.which("codegraph")
    if not cg:
        return {
            **trust,
            "available": False,
            "bundle_root": cwd,
            "note": "codegraph CLI not on PATH; lexical source hits are included instead",
            "fallback_hits": code.search_code(seed, "*.java", 20),
        }
    try:
        result = subprocess.run(
            [cg, "explore", seed],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        ok = result.returncode == 0
        return {
            **trust,
            "available": ok,
            "returncode": result.returncode,
            "bundle_root": cwd,
            "output": result.stdout[:8000],
            "error": result.stderr[:2000],
            "fallback_hits": [] if ok else code.search_code(seed, "*.java", 20),
        }
    except Exception as error:  # noqa: BLE001
        return {
            **trust,
            "available": False,
            "bundle_root": cwd,
            "error": str(error),
            "fallback_hits": code.search_code(seed, "*.java", 20),
        }
```

### 3. Report all defining repos (name-collision detection)

Add a plural helper and make the existing singular one delegate to it — first-wins
behaviour and its docstring intent are preserved.

```python
def _defining_repos(seed):
    """Every repo that owns a `<Symbol>.java` definition file for this seed (usually 0 or 1; >1
    means a name collision that makes bare-symbol routing ambiguous). De-duplicated, in hit order."""
    try:
        hits = code.search_code(seed, "*.java", 40)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(hits, list):
        return []
    want_file = (str(seed).split(".")[-1] + ".java").lower()
    repos = []
    for hit in hits:
        repo, path = _repo_of_hit(hit)
        if repo and os.path.basename(path).lower() == want_file and repo not in repos:
            repos.append(repo)
    return repos


def _defining_repo(seed):
    """The repo that DEFINES a bare symbol (owns `<Symbol>.java`). First match wins; `""` when the
    seed isn't a symbol with a definition file in the mirror (so a repo id never false-matches)."""
    repos = _defining_repos(seed)
    return repos[0] if repos else ""
```

### 4. Thread the defining list through `_resolve_dep_seed` → `query`

`_resolve_dep_seed` now returns the defining list as a third element (so `query`
doesn't run `search_code` twice). **This changes its arity — update its only caller
(and any test that unpacks it).**

```python
def _resolve_dep_seed(seed):
    """Return (repo_for_deps_and_messages, resolution_note_or_None, defining_repos).

    A seed that is already a repo is used as-is (empty defining list). A bare symbol is routed to
    the repo that DEFINES it; `defining_repos` carries ALL repos with a matching definition file so
    the caller can flag a name collision. An unroutable seed is returned unchanged.
    """
    if seed in _known_repos():
        return seed, None, []
    repos = _defining_repos(seed)
    if repos:
        return repos[0], {
            "symbol": seed,
            "resolved_repo": repos[0],
            "via": "definition file in the read-only mirror",
            "note": "dependency/message sections below are for the repo that defines this symbol.",
        }, repos
    return seed, None, []
```

In `query`, update the unpack and add the ambiguity block:

```python
    dep_seed, resolution, defining = _resolve_dep_seed(seed)
    ...
    result = { ... }                      # unchanged
    if resolution:
        result["resolution"] = resolution
    if len(defining) > 1:
        result["routing_ambiguity"] = {
            "symbol": seed,
            "defining_repos": defining,
            "chosen": dep_seed,
            "note": (
                "More than one repo defines a file named after this symbol; deps/messages and the "
                "call graph were routed to ONE of them. Confirm you mean this symbol in this repo — "
                "a same-name class in another repo would return different callers. Disambiguate by "
                "passing a fully-qualified name or the owning repo as the seed."
            ),
        }
    return result
```

(`bundle_root_for` / `_symbol_defining_root` routing is untouched — the block only
*reports* the ambiguity the existing first-wins routing already resolves.)

## Keep working

- `available` / `returncode` / `bundle_root` / `fallback_hits` / `output` / `error`
  keep their exact meaning and values; the four trust keys are additive.
- `call_graph` (raw) inherits `caveat` + freshness because it calls `_call_graph`.
- `_defining_repo` returns the same value as before for every input.
- `agent.answer()` contract (`{answer, tool_trace, usage}`) is unchanged; no server
  or UI change. `LLM_MOCK` path unaffected.

## Done when

1. `unified_impact.query("<any symbol>")["callers"]` contains `caveat` (non-empty),
   `index_built_at`, `indexes_refreshed_at`, and `possibly_stale` — in the success,
   codegraph-missing, AND exception branches.
2. A symbol whose `<Symbol>.java` exists in ≥2 repos yields
   `result["routing_ambiguity"]` listing every defining repo + `chosen`; a
   single-definition symbol and a plain repo seed do NOT get the block.
3. `possibly_stale` is `true` only when both timestamps are present and
   `indexes_refreshed_at > index_built_at`; it is `false` (never a crash) when
   `last_indexed.json` or the DB is absent.
4. Existing routing/behaviour is unchanged: `available`, `fallback_hits`,
   `bundle_root`, deps, and message peers match pre-change output for a repo seed.
5. `tests/test_unified_impact_routing.py` passes (updated for the new arity/keys).

## Verification steps (internal Codex, real mirror)

```bash
# Caveat + freshness present on the flagship tool
python -B -c "from retriever import unified_impact as u; c=u.query('IngressService')['callers']; \
print('caveat?', bool(c.get('caveat'))); \
print('built_at=', c.get('index_built_at'), 'refreshed=', c.get('indexes_refreshed_at'), 'stale=', c.get('possibly_stale'))"

# Ambiguity surfaces for a known collision (pick a class name you know exists in >1 repo),
# and is ABSENT for a repo seed
python -B -c "from retriever import unified_impact as u; \
print('ambig:', u.query('IngressService').get('routing_ambiguity', {}).get('defining_repos')); \
r=u.query('mc-hk-hase-ingress-api'); print('repo-seed ambig (want None):', r.get('routing_ambiguity')); \
print('deps non-empty:', bool(r['dependency_edges']['depended_on_by'] or r['dependency_edges']['depends_on']))"

# Raw call_graph inherits the caveat too
python -B -c "from retriever import unified_impact as u; print('raw caveat?', bool(u.call_graph('IngressService').get('caveat')))"

# Existing tests
python -B -m pytest tests/test_unified_impact_routing.py -q   # or the runner this repo uses
```

Expect: caveat truthy everywhere; `routing_ambiguity.defining_repos` lists ≥2 repos
for the collision seed and is `None` for the repo seed; deps still non-empty for the
repo seed; tests green.

## Notes

- If `search_code` is a hot path in your run, note `_defining_repos` is called once
  per symbol query (repo seeds skip it via the `_known_repos()` short-circuit). No
  extra search vs. today for repo seeds; one bounded search for symbol seeds, same
  as before — the plural helper replaces, not adds to, the singular one's search.
- Do not fold `possibly_stale` into `available` — a stale-but-successful index must
  still return `available: true`; staleness is advisory, not a failure.
- Grep for other callers of `_resolve_dep_seed` before changing its arity
  (`grep -n _resolve_dep_seed retriever/ tests/`); today it's only `query` + tests.
- The four trust keys are safe to add to the MCP tool docstring later
  (`mcp_server.py` `unified_impact` / `call_graph`) so external agents know to read
  them — optional, out of scope here.
