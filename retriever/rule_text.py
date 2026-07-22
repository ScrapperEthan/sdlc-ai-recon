"""Round B1/B2 — tbl_use_case_ext.rule_text tokenizer + structural AST, and the operator-semantics
config seam.

RUNBOOK-45 Part B evidence proved rule_text is NOT a single-truth parser: for `I0141`/`I0142` the
rule_text `LETTER > (EMAIL & SMS)` disagrees with BOTH the channel_rule priority order
(LETTER=1/EMAIL=2/SMS=3) and the Portal composer output (`LETTER > EMAIL > SMS`), and the current
runtime parser is itself buggy (`contains("\\|")` matches a literal backslash, not the regex `|`).
So this module deliberately does NOT assert what `>`/`&`/`|` operationally mean:

  - `parse()` (B1) only builds a STRUCTURAL operator tree — mode/channels/normalized_expression —
    and never emits `initial_channels`/`fallback_edges` as fact.
  - `interpret()` (B2) turns that structure into initial/parallel/fallback/selectable channels, but
    ONLY once the relevant operators are confirmed in `index/rule_text_semantics.json`. Until then
    it returns `{"available": False, ...}`. This file is the single seam an owner answer plugs into
    — filling it in lights up interpretation with zero code change here.

Grammar (EBNF, from the UAT rule_text corpus — operator counts on real data: non-blank 2,640;
no-operator 1,977; `>` 310; `&` 193; `|` 52; mixed 108):

    expr        ::= selectable
    selectable  ::= fallback ("|" fallback)*
    fallback    ::= parallel (">" parallel)*
    parallel    ::= atom ("&" atom)*
    atom        ::= CHANNEL | "(" expr ")"

Never crashes: any malformed rule_text yields a `parse_warnings` entry, not an exception.
"""
import json
import re

from . import config

# The DB channel vocabulary (an unknown token is a parse_warning, never a crash). PUSH+INBOX (DB
# value) vs PUSH_INBOX (Java enum) is a known naming drift (RUNBOOK-45 Part B evidence) — accept
# both spellings as valid atoms rather than picking one.
KNOWN_CHANNELS = {
    "SMS", "EMAIL", "PUSH", "LETTER", "WHATSAPP", "WECHAT", "MMS",
    "TWOWAYSMS", "INAPP", "PUSH_INBOX", "PUSH+INBOX",
}

_TOKEN_RE = re.compile(r"[()&|>]|[A-Za-z0-9_+]+|\S")
_PRECEDENCE = {"|": 1, ">": 2, "&": 3}


# ---------------------------------------------------------------------------
# B1 — tokenizer + recursive-descent parser -> structural AST
# ---------------------------------------------------------------------------

class _ParseError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class _Tokens:
    def __init__(self, text):
        self._tokens = _TOKEN_RE.findall(text)
        self._pos = 0

    def peek(self):
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def advance(self):
        tok = self.peek()
        self._pos += 1
        return tok

    def done(self):
        return self._pos >= len(self._tokens)


def _left_fold(items, op):
    """items is 1+ sub-nodes already parsed at the next-tighter grammar level; folds them
    left-associatively under `op` when there's more than one, otherwise returns the single item
    unchanged (so a level with no operator present is a no-op pass-through, per the grammar)."""
    node = items[0]
    for nxt in items[1:]:
        node = {"op": op, "left": node, "right": nxt}
    return node


def _parse_selectable(tokens, warnings, occurrences, ops_used, grouped_ids):
    items = [_parse_fallback(tokens, warnings, occurrences, ops_used, grouped_ids)]
    while tokens.peek() == "|":
        tokens.advance()
        ops_used.add("|")
        items.append(_parse_fallback(tokens, warnings, occurrences, ops_used, grouped_ids))
    return _left_fold(items, "|")


def _parse_fallback(tokens, warnings, occurrences, ops_used, grouped_ids):
    items = [_parse_parallel(tokens, warnings, occurrences, ops_used, grouped_ids)]
    while tokens.peek() == ">":
        tokens.advance()
        ops_used.add(">")
        items.append(_parse_parallel(tokens, warnings, occurrences, ops_used, grouped_ids))
    return _left_fold(items, ">")


def _parse_parallel(tokens, warnings, occurrences, ops_used, grouped_ids):
    items = [_parse_atom(tokens, warnings, occurrences, ops_used, grouped_ids)]
    while tokens.peek() == "&":
        tokens.advance()
        ops_used.add("&")
        items.append(_parse_atom(tokens, warnings, occurrences, ops_used, grouped_ids))
    return _left_fold(items, "&")


def _parse_atom(tokens, warnings, occurrences, ops_used, grouped_ids):
    tok = tokens.peek()
    if tok is None:
        raise _ParseError("syntax_error", "expected a channel or '(' but reached end of expression")
    if tok == "(":
        tokens.advance()
        node = _parse_selectable(tokens, warnings, occurrences, ops_used, grouped_ids)
        if tokens.peek() != ")":
            raise _ParseError("unbalanced_parens", "missing closing ')'")
        tokens.advance()
        # Remember explicit source grouping (by object identity, not stored in the tree itself, so
        # the public operator_tree stays a clean {"op"/"left"/"right"} or {"channel"} shape) so the
        # printer can preserve human-written parens even where precedence wouldn't strictly require
        # them (e.g. "LETTER > (EMAIL & SMS)" reads clearer than the equally-valid "LETTER > EMAIL &
        # SMS" — both parse identically, but the explicit grouping communicates intent).
        if "op" in node:
            grouped_ids.add(id(node))
        return node
    if tok in (")", "&", "|", ">"):
        raise _ParseError("syntax_error", f"unexpected token {tok!r}")
    tokens.advance()
    name = tok.upper()
    if name not in KNOWN_CHANNELS:
        warnings.append({"type": "unknown_channel", "token": tok})
    occurrences.append(name)
    return {"channel": name}


def _ops_in_tree(node):
    if node is None or "channel" in node:
        return set()
    return {node["op"]} | _ops_in_tree(node.get("left")) | _ops_in_tree(node.get("right"))


def _mode(ops_used):
    if not ops_used:
        return "SINGLE"
    if ops_used == {"|"}:
        return "UPSTREAM_SELECTED"
    if ops_used == {">"}:
        return "FALLBACK"
    if ops_used == {"&"}:
        return "PARALLEL"
    return "MIXED"


def _print_node(node, parent_op, is_right_child, grouped_ids):
    if "channel" in node:
        return node["channel"]
    op = node["op"]
    text = (f"{_print_node(node['left'], op, False, grouped_ids)} {op} "
            f"{_print_node(node['right'], op, True, grouped_ids)}")
    if parent_op is None:
        return text
    needs_parens = (
        id(node) in grouped_ids  # explicit source grouping, always preserved
        or _PRECEDENCE[op] < _PRECEDENCE[parent_op]  # binds looser than context -> ambiguous without parens
        or (_PRECEDENCE[op] == _PRECEDENCE[parent_op] and is_right_child)  # same-op right child != left-fold
    )
    return f"({text})" if needs_parens else text


def parse(rule_text):
    """The structural AST for one rule_text value. NEVER raises — any malformed input yields
    `parse_warnings` entries and a best-effort (possibly `None`) `operator_tree`, not a crash.
    `semantics` is always "unconfirmed" here; call `interpret()` for operational meaning."""
    text = (rule_text or "").strip()
    if not text:
        return {"mode": "EMPTY", "channels": [], "operator_tree": None,
                "normalized_expression": "", "semantics": "unconfirmed", "parse_warnings": []}

    warnings = []
    if "\\" in text:
        # Same bug class as the runtime's `contains("\\|")` — a literal backslash in the raw text,
        # not an escape sequence the grammar understands.
        warnings.append({"type": "literal_escape_artifact",
                          "detail": "raw text contains a literal backslash character"})

    tokens = _Tokens(text)
    occurrences = []
    ops_used = set()
    grouped_ids = set()
    tree = None
    try:
        tree = _parse_selectable(tokens, warnings, occurrences, ops_used, grouped_ids)
        if not tokens.done():
            trailing = tokens.peek()
            kind = "unbalanced_parens" if trailing == ")" else "syntax_error"
            warnings.append({"type": kind, "detail": f"unexpected trailing token {trailing!r}"})
            tree = None
    except _ParseError as error:
        warnings.append({"type": error.kind, "detail": str(error)})
        tree = None

    counts = {}
    for name in occurrences:
        counts[name] = counts.get(name, 0) + 1
    for name, count in counts.items():
        if count > 1:
            warnings.append({"type": "duplicate_channel", "channel": name})

    return {
        "mode": _mode(ops_used),
        "channels": sorted(set(occurrences)),
        "operator_tree": tree,
        "normalized_expression": _print_node(tree, None, False, grouped_ids) if tree else text,
        "semantics": "unconfirmed",
        "parse_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# B2 — operator-semantics config: owner-confirmed, default "flag, don't guess"
# ---------------------------------------------------------------------------

DEFAULT_SEMANTICS = {
    ">": {"meaning": "unconfirmed"},
    "&": {"meaning": "unconfirmed"},
    "|": {"meaning": "unconfirmed"},
    "precedence": ["|", ">", "&"],
    "confirmed_by": None,
    "confirmed_at": None,
}


def load_semantics():
    """index/rule_text_semantics.json, merged over the safe default. Missing/invalid file -> the
    default (every operator unconfirmed) — never a crash."""
    try:
        with open(config.RULE_TEXT_SEMANTICS_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        data = None
    merged = dict(DEFAULT_SEMANTICS)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def _op_confirmed(semantics, op):
    meaning = ((semantics or {}).get(op) or {}).get("meaning")
    return meaning not in (None, "unconfirmed")


def _flatten_group(node):
    """All channels reachable via nested '&' from this node — the flat list of channels grouped
    together at this position. Purely structural (see stage_groups' docstring)."""
    if "channel" in node:
        return [node["channel"]]
    if node["op"] == "&":
        return _flatten_group(node["left"]) + _flatten_group(node["right"])
    return [f"<{node['op']}-subexpression>"]  # a different operator nested here via explicit parens


def stage_groups(tree):
    """Left-to-right list of channel-groups for a tree's top '>'/'&' structure — e.g.
    "LETTER > (EMAIL & SMS)" -> [["LETTER"], ["EMAIL", "SMS"]].

    PURELY STRUCTURAL: this describes the tree's SHAPE (which channels are grouped by '&' vs
    separated by '>'), not what those operators operationally mean — safe to use without any
    `rule_text_semantics.json` confirmation (contrast with `interpret()`, which is semantics-gated).
    Used by Round B3's consistency validator to compare rule_text's grouping against the
    channel_rule priority ordering without asserting fallback/parallel meaning.
    """
    if tree is None:
        return []
    if "channel" in tree:
        return [[tree["channel"]]]
    if tree["op"] == ">":
        return stage_groups(tree["left"]) + stage_groups(tree["right"])
    if tree["op"] == "&":
        return [_flatten_group(tree)]
    return [[f"<{tree['op']}-subexpression>"]]  # e.g. a bare '|' node reached directly (unusual)


def _build_interpretation(tree, semantics):
    if tree.get("op") is None:
        return {"available": True, "initial_channels": [tree["channel"]], "parallel_groups": [],
                "fallback_edges": [], "selectable_channels": []}
    if tree["op"] == "|":
        options = []

        def _collect(node):
            if node.get("op") == "|":
                _collect(node["left"])
                _collect(node["right"])
            else:
                options.append(node)

        _collect(tree)
        selectable = sorted({ch for opt in options for stage in stage_groups(opt) for ch in stage})
        return {"available": True, "initial_channels": [], "parallel_groups": [],
                "fallback_edges": [], "selectable_channels": selectable}
    stages = stage_groups(tree)
    initial = stages[0] if stages else []
    edges = [[frm, to] for frm_stage, to_stage in zip(stages, stages[1:])
             for frm in frm_stage for to in to_stage]
    parallel_groups = [stage for stage in stages if len(stage) > 1]
    return {"available": True, "initial_channels": initial, "parallel_groups": parallel_groups,
            "fallback_edges": edges, "selectable_channels": []}


def interpret(ast, semantics=None):
    """initial_channels / parallel_groups / fallback_edges / selectable_channels — ONLY when every
    operator actually used in `ast["operator_tree"]` is confirmed in `index/rule_text_semantics.json`.
    While unconfirmed (the default — always true until an owner edits that file), returns
    `{"available": False, "reason": ..., "unconfirmed_operators": [...]}`. Never guesses."""
    semantics = load_semantics() if semantics is None else semantics
    tree = (ast or {}).get("operator_tree")
    if not tree:
        return {"available": False, "reason": "no parseable structure"}
    unconfirmed = sorted(op for op in _ops_in_tree(tree) if not _op_confirmed(semantics, op))
    if unconfirmed:
        return {"available": False, "reason": "operator semantics not owner-confirmed",
                "unconfirmed_operators": unconfirmed}
    return _build_interpretation(tree, semantics)
