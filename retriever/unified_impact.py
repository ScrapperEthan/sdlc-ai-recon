"""Unified blast-radius view across deps, async messages, and code evidence.

Once CodeGraph indexes are built per bundle (see ``build_codegraph.py``), ``codegraph explore``
must run in the *right* bundle's staging root — not the process cwd. ``bundle_root_for`` resolves
that root from the build manifest; when nothing is built (or the seed can't be routed) it returns
``None`` and we fall back to the pre-build cwd behaviour, so nothing regresses.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

from . import code, config, graph, messages, repo_tags

# A search hit is "path:line:text"; capture the path up to the .java (robust to colons in text
# and to a Windows drive letter) so a bare symbol can be routed to the repo that defines it.
_HIT_RE = re.compile(r"^(?P<path>.*?\.java):\d+:", re.IGNORECASE)

# Stamped into every call-graph result so an agent using the MCP tool without the
# QA system prompt still knows not to treat these edges as the whole truth.
CALL_GRAPH_CAVEAT = (
    "Candidate call edges from a STATIC index. Reflection, Spring AOP/proxies, runtime DI, and "
    "config/DB-driven dispatch may be MISSING or wrong. Async (MQ/topic) flows are NOT here — see "
    "message_edges. Treat each edge as a lead: confirm it in source (search_code/read_file) and "
    "cite repo/path/File.java:line before asserting it."
)


def _iso(epoch):
    """UTC ISO-8601 (Z, no microseconds), matching refresh.py's ``_now()`` shape."""
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _index_freshness(cwd):
    """Return freshness for the CodeGraph index queried at ``cwd``.

    ``possibly_stale`` means the derived indexes were refreshed after this CodeGraph database was
    built, so the mirror may have moved on and callers should double-check source. It is never
    guessed: unavailable timestamps produce ``False``.
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


def _message_peers(seed):
    peers = []
    for edge in messages.routes_for_repo(seed):
        producer = edge.get("producer_repo") or ""
        consumer = edge.get("consumer_repo") or ""
        if producer == seed and consumer:
            direction = "produces_to_consumer"
            peer = consumer
        elif consumer == seed and producer:
            direction = "consumes_from_producer"
            peer = producer
        else:
            direction = "message_edge"
            peer = producer or consumer
        peers.append(
            {
                "direction": direction,
                "peer_repo": peer,
                "destination": edge.get("destination") or "",
                "routing_source": edge.get("routing_source") or "",
                "evidence": edge.get("evidence") or config.MESSAGE_EDGES_CSV,
            }
        )
    return peers


def _built_roots():
    """{bundle: staging_root} for manifest entries that built cleanly (returncode == 0)."""
    try:
        with open(config.CODEGRAPH_BUILD_JSON, encoding="utf-8-sig") as handle:
            manifest = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    roots = {}
    for entry in (manifest or {}).get("bundles") or []:
        if isinstance(entry, dict) and entry.get("returncode") == 0 and entry.get("root"):
            roots[entry.get("bundle")] = entry["root"]
    return roots


def _repo_of_hit(hit):
    """The repo (first path segment under the mirror) for a 'path:line:text' search hit."""
    match = _HIT_RE.match(hit or "")
    if not match:
        return "", ""
    path = match.group("path")
    try:
        rel = os.path.relpath(path, config.MIRROR).replace("\\", "/")
    except ValueError:
        return "", path
    if rel.startswith(".."):
        return "", path
    return rel.split("/", 1)[0], path


def _symbol_defining_root(seed, roots):
    """Route a bare symbol to a built bundle via the repo that defines it: search the mirror for the
    symbol, prefer a hit in ``<Symbol>.java`` (the definition), and use that repo's bundle root."""
    try:
        hits = code.search_code(seed, "*.java", 40)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(hits, list):
        return None
    want_file = (str(seed).split(".")[-1] + ".java").lower()
    fallback = None
    for hit in hits:
        repo, path = _repo_of_hit(hit)
        if not repo:
            continue
        root = roots.get((repo_tags.for_repo(repo).get("bundle") or "").strip())
        if not root:
            continue
        if os.path.basename(path).lower() == want_file:
            return root  # the definition file — strongest signal
        if fallback is None:
            fallback = root
    return fallback


def _known_repos():
    """The repo universe used to decide whether a seed is already a repo (skip symbol routing).
    Union of the dependency-graph nodes and the repo_tags universe; empty when no data is present
    (in which case a seed is treated as a repo, i.e. exactly the pre-resolution behaviour)."""
    repos = set()
    try:
        repos |= graph.known_repos()
    except Exception:  # noqa: BLE001 — missing data must not break routing
        pass
    try:
        repos |= set(repo_tags.load().keys())
    except Exception:  # noqa: BLE001
        pass
    return repos


def _defining_repos(seed):
    """Every repo owning a ``<Symbol>.java`` definition file, de-duplicated in hit order.

    Usually this returns zero or one repo; multiple results indicate a same-name symbol collision
    that makes bare-symbol routing ambiguous.
    """
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
    """The repo that DEFINES a bare symbol — first match wins.

    Only a definition-file match counts, so a repo id (which has no ``<id>.java``) never
    false-matches and gets its dependencies rerouted. Returns ``""`` when no definition exists.
    """
    repos = _defining_repos(seed)
    return repos[0] if repos else ""


def _resolve_dep_seed(seed):
    """Return ``(repo_for_deps_and_messages, resolution_note_or_None, defining_repos)``.

    A seed that is already a repo is used as-is. A bare symbol is routed to the first repo that
    DEFINES it, so ``dependency_edges``/``message_edges`` (which key on repo, not symbol) aren't
    empty. The defining list makes same-name routing ambiguity visible without changing routing.
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


def bundle_root_for(seed, bundle=None):
    """Resolve the staging root to run ``codegraph explore`` in for this seed.

    (1) explicit ``bundle`` arg if built; else (2) if ``seed`` is a repo in repo_tags.json, its
    ``bundle`` field's root; else (3) a built bundle whose staging dir contains a ``<seed>`` repo
    dir; else (4) for a bare symbol, the bundle of the repo that defines it; else ``None`` (caller
    falls back to the process cwd — back-compatible pre-build).
    """
    roots = _built_roots()
    if not roots:
        return None
    if bundle and bundle in roots:
        return roots[bundle]
    tagged = (repo_tags.for_repo(seed).get("bundle") or "").strip()
    if tagged and tagged in roots:
        return roots[tagged]
    for root in roots.values():
        if os.path.isdir(os.path.join(root, seed)):
            return root
    return _symbol_defining_root(seed, roots)


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


def call_graph(query_text):
    """Raw routed ``codegraph explore <symbol>``: resolve the symbol's bundle, then explore there.

    Single source of truth so every entry point (webapp, MCP, CLI) routes identically instead of
    each re-deriving the bundle root — the previous drift where only the webapp had this wired.
    """
    root = bundle_root_for(query_text)
    return _call_graph(query_text, cwd=root)


def query(seed, transitive=False, bundle=None):
    """Return deps + async peers + callers/source hits for a repo or symbol.

    ``bundle`` is an optional routing hint; without it the seed routes by its repo tag (if it is a
    repo) or by which built staging dir contains it. An unroutable seed uses the process cwd.
    """
    seed = (seed or "").strip()
    if not seed:
        return {"error": "seed is required"}

    # Deps and message edges key on repo names; a bare symbol seed would return empty from both.
    # Route them through the repo that defines the symbol. The call graph still runs on the raw
    # seed (codegraph explores the symbol itself), routed to that repo's built bundle.
    dep_seed, resolution, defining = _resolve_dep_seed(seed)
    dep = graph.impact(dep_seed, transitive=transitive)
    root = bundle_root_for(seed, bundle=bundle)
    result = {
        "seed": seed,
        "resolved_repo": dep_seed if dep_seed != seed else None,
        "bundle_root": root,
        "citation_contract": (
            "Every caller/callee named below must be cited as repo/path/File.java:line. "
            "The call graph often gives only the file — when it does, search_code the called "
            "member or read_file that caller to pin the exact line BEFORE answering; never "
            "emit a file-only citation or defer the line to a follow-up."
        ),
        "dependency_edges": {
            "source": config.EDGES_CSV,
            "repo": dep_seed,
            "mode": dep["mode"],
            "depended_on_by": dep["depended_on_by"],
            "depends_on": dep["depends_on"],
        },
        "message_edges": {
            "source": config.MESSAGE_EDGES_CSV,
            "repo": dep_seed,
            "peers": _message_peers(dep_seed),
        },
        "callers": _call_graph(seed, cwd=root),
    }
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
