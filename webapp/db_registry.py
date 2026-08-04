"""What we are allowed to ask the read-only UAT database, and what it is called over there.

The seam, identical in shape to `mcp_registry`: this side owns the ABSTRACT query names
(`usecase_routing_live`, `topic_usecase_map`, …) and the code refers only to those.
`config/db_queries.json` — intranet-owned — holds the real SQL, schema, table and column names. A
table rename is a config edit on the box, not an engine change waiting on external development.

The model never writes SQL. It picks a declared name and supplies bound parameters; everything that
decides WHAT is read and WHICH COLUMNS may leave the database lives in the config. That is not only
an injection defence — `columns` is an allow-list, so a PII column that is never listed can never
reach a model or a browser, which is a stronger guarantee than redacting it after the fact.

Four things worth being explicit about:

* **Allow-list, not SQL.** Only queries declared in the config may run. There is no path from model
  text to a statement, so there is nothing for a prompt injection to steer.
* **The statement gate has ONE definition.** `statement_problems()` is called when a query is built
  AND again immediately before the runner is invoked. Two copies of the same predicate are two
  things that drift, and the shape they drift into is exactly RUNBOOK-71: the planning layer said
  "runnable" and the executing layer agreed just long enough to open a connection.
* **Unwired fails closed and says which field is missing.** A `"?"` placeholder means "not filled in
  yet". Crucially, an unwired query costs ZERO database calls — we do not connect and then discover
  there was nothing to ask.
* **Default closed.** `caller_policy` defaults to `internal`, so wiring a query does not by itself
  expose it to the chat model. Unlike the MCP scope knobs — which default OPEN because they narrow
  behaviour that already existed — this whole path is new, so an absent setting can safely mean "no".
"""
import json
import os
import re

from retriever import config as retriever_config

UNSET = "?"

# Who may run a query. `internal` is the default: wiring the SQL and exposing it to the chat model
# are two separate decisions, and the second one should have to be typed.
CALLER_PRODUCT, CALLER_INTERNAL, CALLER_DISABLED = "product", "internal", "disabled"
CALLER_POLICIES = (CALLER_PRODUCT, CALLER_INTERNAL, CALLER_DISABLED)


class NotWired(RuntimeError):
    """The query is declared but its config still has placeholders."""


class NotAllowed(RuntimeError):
    """Unknown, disabled, out of the caller's reach, or a statement the gate refuses."""


TEMPLATE_NAME = "db_queries.json"
LOCAL_NAME = "db_queries.local.json"


def template_path():
    return os.path.join(retriever_config.ROOT, "config", TEMPLATE_NAME)


def config_source():
    """`(path, kind)` for the config actually in effect — `env`, `local` or `template`.

    The middle step is the whole point. The intranet CANNOT PUSH, so a config they edit in place
    has to be a file git never touches: otherwise their edit sits as an uncommitted change to a
    tracked file, and the next `git pull` that also updates that file is refused outright — one
    config file blocking every unrelated fix in the same pull.

    Requiring an env var as well would be one more thing to forget on a box that gets rebuilt, so
    the conventional filename is enough on its own. `SDLC_DB_QUERIES` still wins when set, for a
    deployment that keeps its config somewhere else entirely.

    Replacement, not merge: the file in effect is the whole config. That is why every default lives
    in code — RUNBOOK-58 is what happens when a replacement file silently drops a safety list.
    """
    explicit = (os.environ.get("SDLC_DB_QUERIES") or "").strip()
    if explicit:
        return explicit, "env"
    local = os.path.join(retriever_config.ROOT, "config", LOCAL_NAME)
    if os.path.isfile(local):
        return local, "local"
    return template_path(), "template"


def _config_path():
    return config_source()[0]


def _read(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"runner": {}, "queries": {}, "_load_error": f"{path}: {exc}"}
    if not isinstance(payload, dict):
        return {"runner": {}, "queries": {},
                "_load_error": f"{path}: expected a JSON object at the top level"}
    return payload


def load():
    """Unreadable config degrades to nothing callable — but carries the reason.

    Same lesson as `mcp_registry.load`: a typo in the env var override used to be indistinguishable
    from "no queries exist", which costs someone a round trip to find out."""
    return _read(_config_path())


def wired_names(cfg):
    """Query names that have a real statement rather than a placeholder."""
    return sorted(name for name, spec in queries(cfg).items()
                  if isinstance(spec, dict) and spec.get("sql", UNSET) != UNSET)


def config_health(cfg=None):
    """Is the config in a state that will survive the next `git pull`, and does it still match the
    template? Both are invisible failures otherwise — one shows up as a blocked pull weeks later,
    the other as a query the box has simply never heard of.
    """
    path, kind = config_source()
    cfg = cfg if cfg is not None else _read(path)
    out = {"path": path, "source": kind, "warnings": []}
    wired = wired_names(cfg)
    if kind == "template" and wired:
        out["warnings"].append(
            f"{path} is the git-TRACKED template and it has been wired ({', '.join(wired)}). Copy "
            f"it to config/{LOCAL_NAME} (gitignored) and edit that instead — an uncommitted change "
            f"to the tracked file makes the next `git pull` refuse to update it, which blocks the "
            f"whole pull, and this side cannot push a fix back.")
    if kind != "template":
        template = _read(template_path())
        missing = sorted(set(queries(template)) - set(queries(cfg)))
        if missing:
            out["warnings"].append(
                f"named queries added to the template since this config was copied are absent "
                f"here: {', '.join(missing)}. They cannot be called until they are added.")
        out["template_only"] = missing
    return out


def _clean(mapping):
    """Config dicts carry `_note`/`_what`/`_README` documentation keys; they are never data."""
    if not isinstance(mapping, dict):
        return {}
    return {k: v for k, v in mapping.items() if not str(k).startswith("_")}


def runner(cfg=None):
    return _clean((cfg or load()).get("runner"))


def queries(cfg=None):
    return _clean((cfg or load()).get("queries"))


def caller_policy(name, cfg=None):
    spec = queries(cfg).get(name) or {}
    declared = str(spec.get("caller_policy") or "").strip().lower()
    return declared if declared in CALLER_POLICIES else CALLER_INTERNAL


# --------------------------------------------------------------------------------------------
# The statement gate.
#
# Their runner enforces its own version of all of this (RUNBOOK-72 §2), and theirs is the one that
# actually protects the database. This one exists because we should not be SENDING a statement we
# would not accept back — a request that only their side refuses is a request we should never have
# made, and the refusal arrives too late to tell us anything we did not already know.
# --------------------------------------------------------------------------------------------

# Whole words only. `offset` does not match `\bset\b`, which is why these are word-anchored rather
# than substring checks.
_FORBIDDEN_WORDS = (
    "insert", "update", "delete", "merge", "truncate", "drop", "create", "alter", "grant",
    "revoke", "copy", "call", "vacuum", "analyze", "reindex", "cluster", "refresh", "lock",
    "begin", "commit", "rollback", "savepoint", "listen", "notify", "prepare", "execute",
    "declare", "fetch", "discard", "reset", "into", "returning", "nextval", "setval",
)
_FORBIDDEN_FUNCTIONS = (
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "pg_terminate_backend", "pg_cancel_backend",
    "set_config", "pg_reload_conf", "current_setting",
)
_CTE = re.compile(r"(?:\bwith\b|,)\s+([a-zA-Z_]\w*)\s+as\s*(?:materialized\s*|not\s+materialized\s*)?\(",
                  re.IGNORECASE)
_RELATION = re.compile(
    r"\b(?:from|join)\s+(?!\()(?:only\s+|lateral\s+)*([a-zA-Z_][\w$]*(?:\.[a-zA-Z_][\w$]*)*)",
    re.IGNORECASE)
_NAMED_PARAM = re.compile(r"%\(([A-Za-z_]\w*)\)s")
_POSITIONAL_PARAM = re.compile(r"%s")


def allowed_schemas(cfg=None):
    """The schemas a named query may read, lower-cased. Empty means "qualified, but unchecked".

    Filled in, this is a scope gate rather than a syntax check — the same distinction the LogDream
    audit turned on: a table name that happens to resolve is not a table we were allowed to read,
    and a one-letter typo in a schema resolves to a real, different place.
    """
    raw = (cfg or load()).get("schemas")
    if not isinstance(raw, list):
        return []
    return [str(s).strip().lower() for s in raw if str(s).strip() not in ("", UNSET)]


def statement_problems(sql, schemas=None):
    """Every reason this statement is not acceptable, as a list. Empty list means acceptable.

    Returns all of them rather than the first: a config author fixing one placeholder at a time
    should not need one round trip per problem.
    """
    schemas = [str(s).lower() for s in (schemas or [])]
    problems = []
    text = (sql or "").strip()
    if not text:
        return ["empty statement"]
    body = text[:-1].strip() if text.endswith(";") else text
    if ";" in body:
        problems.append("more than one statement (';' inside the body)")
    if not re.match(r"^(select|with)\b", body, re.IGNORECASE):
        problems.append("must start with SELECT or WITH")
    if "--" in body or "/*" in body or "*/" in body:
        problems.append("SQL comments are not accepted (they can hide the rest of the statement)")
    if "$$" in body:
        problems.append("dollar-quoted strings are not accepted")
    lowered = body.lower()
    for word in _FORBIDDEN_WORDS:
        if re.search(r"\b" + word + r"\b", lowered):
            problems.append(f"forbidden keyword: {word.upper()}")
    for func in _FORBIDDEN_FUNCTIONS:
        if re.search(r"\b" + func + r"\s*\(", lowered):
            problems.append(f"forbidden function: {func}()")
    if re.search(r"\bselect\s+\*", lowered) or re.search(r"\.\s*\*", lowered):
        problems.append("SELECT * is not accepted — name the columns")
    if re.search(r"\bfor\s+(update|share|no\s+key\s+update)\b", lowered):
        problems.append("locking reads are not accepted")
    if _POSITIONAL_PARAM.search(_NAMED_PARAM.sub("", body)):
        problems.append("positional %s parameters are not accepted — use %(name)s")
    known_ctes = {match.lower() for match in _CTE.findall(body)}
    for relation in _RELATION.findall(body):
        if "." not in relation:
            if relation.lower() not in known_ctes:
                problems.append(f"relation {relation!r} is not schema-qualified (write schema.table)")
            continue
        schema = relation.split(".", 1)[0].lower()
        if schemas and schema not in schemas:
            problems.append(f"schema {schema!r} is outside the configured scope {sorted(schemas)}")
    return problems


def check_statement(sql, cfg=None):
    """Raise `NotAllowed` naming every problem. The single definition both layers call.

    Both layers pass through here — and both let it read the schema scope from the same config —
    so there is no way for the planning side and the executing side to disagree about what is
    acceptable. That disagreement is precisely what RUNBOOK-71 turned out to be.
    """
    problems = statement_problems(sql, allowed_schemas(cfg))
    if problems:
        raise NotAllowed("statement refused: " + "; ".join(problems))


# --------------------------------------------------------------------------------------------


def _missing(spec):
    missing = []
    if spec.get("sql", UNSET) == UNSET:
        missing.append("sql")
    columns = spec.get("columns")
    if not isinstance(columns, list) or not columns or any(c == UNSET for c in columns):
        missing.append("columns")
    return missing


def _coerce(name, declared, value):
    kind = str((declared or {}).get("type") or "string").lower()
    if kind == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise NotAllowed(f"parameter {name!r} must be an integer, got {value!r}") from None
    if kind == "string":
        text = str(value).strip()
        if not text:
            raise NotAllowed(f"parameter {name!r} must not be empty")
        return text
    raise NotWired(f"parameter {name!r} declares an unsupported type {kind!r} "
                   f"(only 'string' and 'integer' are accepted)")


def max_rows(name, cfg=None):
    cfg = cfg or load()
    spec = queries(cfg).get(name) or {}
    cap = runner(cfg).get("max_rows_hard_cap")
    cap = int(cap) if isinstance(cap, int) and cap > 0 else 200
    declared = spec.get("max_rows")
    declared = int(declared) if isinstance(declared, int) and declared > 0 else cap
    return min(declared, cap)


def environment(cfg=None):
    """The one environment this side may address. Never a parameter, never model-supplied.

    Reading it from config rather than hard-coding it means the intranet owns the value — but the
    allow-list is what makes that safe: pointing this at production is a decision that needs its own
    review, not a one-letter config edit.
    """
    spec = runner(cfg)
    allowed = spec.get("allowed_environments")
    allowed = [str(a) for a in allowed] if isinstance(allowed, list) and allowed else ["u"]
    env = str(spec.get("environment") or "u")
    if env not in allowed:
        raise NotAllowed(
            f"runner.environment is {env!r}, which is not in allowed_environments {allowed}. "
            f"Only UAT is wired; anything else needs its own authorisation, column review and "
            f"audit decision — not a config edit.")
    return env


def build_query(name, args=None, cfg=None, caller="product"):
    """Translate one named query into the SQL and bound parameters to execute.

    Raises rather than improvising. An answer built on a guessed column is worse than an answer
    that says the integration is not ready — and much worse here than for logs, because a database
    row looks authoritative in a way a log line does not.
    """
    cfg = cfg or load()
    spec = queries(cfg).get(name)
    if not isinstance(spec, dict):
        raise NotAllowed(
            f"unknown query: {name!r}. Only names declared in config/db_queries.json may run; "
            f"there is no path from free text to SQL.")
    if not spec.get("enabled"):
        raise NotAllowed(f"query {name!r} is declared but not enabled in config/db_queries.json")

    policy = caller_policy(name, cfg)
    if policy == CALLER_DISABLED:
        raise NotAllowed(f"query {name!r} has caller_policy 'disabled': nothing may call it.")
    if policy == CALLER_INTERNAL and caller != CALLER_INTERNAL:
        raise NotAllowed(
            f"query {name!r} is 'internal': available to an authorised internal caller, not to the "
            f"chat model. Set caller_policy to 'product' in config/db_queries.json to open it.")

    missing = _missing(spec)
    if missing:
        raise NotWired(
            f"query {name!r} is not wired yet — {', '.join(missing)} still \"?\" in "
            f"config/db_queries.json. No database call was made.")

    sql = str(spec.get("sql"))
    check_statement(sql, cfg)
    environment(cfg)     # refuse a non-UAT target before anything is bound

    declared = _clean(spec.get("params"))
    supplied = dict(args or {})
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        raise NotAllowed(
            f"query {name!r} does not take {', '.join(repr(u) for u in unknown)}. "
            f"Accepted parameters: {sorted(declared) or 'none'}.")

    placeholders = set(_NAMED_PARAM.findall(sql))
    undeclared = sorted(placeholders - set(declared))
    if undeclared:
        raise NotWired(
            f"query {name!r} has SQL placeholders with no declared parameter: "
            f"{', '.join(undeclared)}. Add them to `params` in config/db_queries.json.")

    params = {}
    for key, declaration in declared.items():
        if key in supplied and supplied[key] is not None:
            params[key] = _coerce(key, declaration, supplied[key])
        elif (declaration or {}).get("required"):
            raise NotAllowed(f"query {name!r} requires parameter {key!r}")
    unfilled = sorted(placeholders - set(params))
    if unfilled:
        raise NotAllowed(
            f"query {name!r} needs values for {', '.join(unfilled)} — the SQL binds them.")

    return {
        "name": name,
        "sql": sql,
        "params": params,
        "max_rows": max_rows(name, cfg),
        "columns": [str(c) for c in spec.get("columns")],
        "source_tables": [str(t) for t in (spec.get("source_tables") or []) if t != UNSET],
        "environment": environment(cfg),
    }


def readiness(cfg=None):
    """Per-query state for the catalog, with no database contact whatsoever."""
    cfg = cfg or load()
    out = {}
    for name, spec in queries(cfg).items():
        if not isinstance(spec, dict):
            continue
        missing = _missing(spec)
        problems = ([] if "sql" in missing
                    else statement_problems(str(spec.get("sql")), allowed_schemas(cfg)))
        if missing:
            state = "not_wired"
        elif problems:
            state = "refused"
        elif not spec.get("enabled"):
            state = "disabled"
        else:
            state = "ready"
        out[name] = {
            "state": state,
            "what": str(spec.get("_what") or ""),
            "params": sorted(_clean(spec.get("params"))),
            "caller_policy": caller_policy(name, cfg),
            "max_rows": max_rows(name, cfg),
            "missing": missing,
            "statement_problems": problems,
        }
    return out
