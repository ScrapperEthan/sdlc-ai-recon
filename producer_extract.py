#!/usr/bin/env python3
"""Signature + wrapper-aware producer detection for the async message map.

The recon (docs/specs/producer-coverage.md) showed producers rarely name their destination as a
string literal (0% in the sample) — they publish through wrapper/base classes and resolve the
topic/queue at runtime from config, constants, or builders. A plain keyword scan therefore finds
almost no producers. This module adds three things the old scan lacked:

1. **Wrapper recognition** — a class that extends/implements a known producer base (or is a
   ``*Producer``/``*EventService``/``*SendService`` family) IS a producer for its repo, even when
   the send is indirect.
2. **Signature-aware send sites** — framework send methods, with a receiver-type guard so a generic
   ``.send(``/``.publish(`` is only counted when the receiver resolves to a messaging type (recon
   §3.2), avoiding the ~174 noisy ``publish`` hits.
3. **A repo-scoped destination resolution ladder** — literal -> constant -> config/@Value(yaml) ->
   builder/getter -> runtime-unresolved. The RUNBOOK-40 real-mirror verify found producer *identity*
   is accurate (0/10 false positives) but every added record had a BLANK destination: the send arg
   is a chained getter (``config.getQueue()``), an ``@Value`` field, or a ``getTopicName()`` whose
   value lives in yaml / a constant / another method — hops a single-line regex cannot cross. v2
   reads the whole repo once (``RepoIndex``) and resolves *within* it: cross-file constants,
   ``@Value`` -> the repo's yaml/properties, and getters whose body returns a resolvable value.
   Anything still unresolved is KEPT (not dropped) as a producer candidate, tagged so a
   human/CodeGraph pass can finish the long tail.

Every record carries ``routing_source``/``confidence``/``resolution_status`` so precision is
auditable. stdlib only, read-only over the mirror. Emits rows that extend ``message_edges.csv``
additively — the existing 5 columns stay, new evidence columns trail them.
"""
import os
import re

# Reused literal detectors + channel/kind helpers (make_message_map does NOT import this module at
# top level, so importing it here is not circular).
from make_message_map import HRN_RE, QUEUE_APP_RE, QUEUE_MQ_RE, _kind

_SKIP = {".git", "target", "build", "node_modules", ".codegraph"}
_TEST_RE = re.compile(r"(^|/)(test|tests|it)(/|$)", re.I)

# Wrapper / base classes a producer commonly extends or implements (recon Q3/§5.2). Seed set —
# identifiers only, no real repo names; extend as the mirror reveals more.
PRODUCER_BASES = (
    "AbstractEventProducer", "AbstractKafkaProducerService", "EventProducer", "IEBProducer",
    "RocketmqEventProducer", "MailProducer", "SqsProducer", "EBKafkaProducer",
    "EBKafkaTransactionProducer", "EBProducer", "KafkaSender", "DelayQueueSender",
)
# Class-name families that denote a producer/sender (recon: *EventService, *SendService, *Producer).
PRODUCER_SUFFIXES = ("EventProducer", "EventService", "SendService", "Producer", "Publisher")

# Framework send methods (recon Q1/§8.1). "send"/"publish" are generic and only counted when the
# receiver is a known messaging type; the explicit names below are trusted on their own.
SEND_METHODS = {
    "convertAndSend", "publishMessage", "publishMessageForEventModel", "publishMessageAndRetry",
    "send", "publish",
}
_TRUSTED_METHODS = {
    "convertAndSend", "publishMessage", "publishMessageForEventModel", "publishMessageAndRetry",
}
# Receiver types that confirm a generic send/publish is really messaging (recon §3.2 guard).
FRAMEWORK_RECEIVER_TYPES = (
    "JmsTemplate", "KafkaTemplate", "MessageProducer", "RocketMQTemplate", "SnsClient",
    "AmazonSNS", "StreamBridge", "KafkaProducer",
)

_CLASS_RE = re.compile(r"\bclass\s+(\w+)\b(.*)")
_CALL_RE = re.compile(r"(?:(\w+)\.)?\b([A-Za-z_]\w*)\s*\(")
_FRAMEWORK_VAR_RE = re.compile(
    r"\b(?:" + "|".join(map(re.escape, FRAMEWORK_RECEIVER_TYPES)) + r")\s+(\w+)"
)


def _iter_java(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for name in filenames:
            if name.endswith(".java"):
                path = os.path.join(dirpath, name)
                if not _TEST_RE.search(path.replace(os.sep, "/")):
                    yield path


def _framework_vars(text):
    """Variable names in a file declared as a framework messaging type (the send receiver guard)."""
    return set(_FRAMEWORK_VAR_RE.findall(text))


def _wrapper_base(line):
    """(class_name, base, confidence) if this line declares a producer wrapper, else None.

    A match on a known base is high confidence; a name-family match (``*Producer`` etc.) is medium.
    """
    match = _CLASS_RE.search(line)
    if not match:
        return None
    name, rest = match.group(1), match.group(2)
    for base in PRODUCER_BASES:
        if re.search(r"\b(?:extends|implements)\b[^{]*\b" + re.escape(base) + r"\b", rest):
            return name, base, "high"
    if name.endswith(PRODUCER_SUFFIXES):
        return name, "", "medium"
    return None


# --- repo-scoped destination resolution (RUNBOOK-42) ---------------------------------------------
# recon distribution: 0% literal, 45% config/@Value/YAML, 32% builder/method-return, 9% constant/
# enum, 14% injected. The send arg is rarely a literal; it is a constant, an @Value field, or a
# getter whose value lives in yaml or another method. The RUNBOOK-40 real-mirror verify confirmed
# this: producers were identified accurately but every added record had a BLANK destination. So the
# ladder must cross those hops. RepoIndex reads one repo's java + yaml once so it can, without
# leaving the repo (stdlib, read-only).

_YML_EXT = (".yml", ".yaml")
_CONST_DEF_RE = re.compile(r'\b([A-Z][A-Z0-9_]{3,})\s*=\s*"([^"]+)"')
_VALUE_FIELD_RE = re.compile(
    r'@Value\(\s*"\$\{([^:}"]+)(?::[^}"]*)?\}"\s*\)[^;{]*?\b([a-z]\w*)\s*[;=]'
)
_GETTER_DEF_RE = re.compile(r'(?<![.\w])(get[A-Z]\w*)\s*\([^)]*\)\s*\{')
_RETURN_RE = re.compile(r'\breturn\s+([^;]+);')
_PROP_LINE_RE = re.compile(r'^\s*([\w.\-]+)\s*=\s*(.+?)\s*$')
_YML_KV_RE = re.compile(r'^(\s*)([\w.\-]+)\s*:\s*(.*)$')
_GETTER_CALL_RE = re.compile(r'\b(get(?:Topic\w*|Queue\w*|Destination\w*))\s*\(')
_IDENT_RE = re.compile(r'^(?:this\.)?([a-z]\w*)$')
# A constant reference, bare (``SMS_TOPIC``) or qualified (``Topics.SMS_TOPIC``).
_CONST_REF_RE = re.compile(r'(?:^|\.)([A-Z][A-Z0-9_]{3,})$')

# Lombok @Getter/@Data classes/fields have NO getter body in source (generated at build time) — the
# RUNBOOK-42 real-mirror re-verify found this is the dominant real-world shape (3/3 spot-checked
# unresolved getters were plain Lombok fields) and v2 missed it entirely, producing zero new
# resolutions on the real mirror despite passing on hand-written-getter fixtures.
_CLASS_DECL_RE = re.compile(r'\bclass\s+(\w+)\b[^{]*\{')
_LOMBOK_GETTER_RE = re.compile(r'@(?:Getter|Data)\b')
_CONFIG_PROPS_RE = re.compile(r'@ConfigurationProperties\(\s*(?:prefix\s*=\s*)?"([^"]+)"')
_FIELD_DEF_RE = re.compile(
    r'(?:private|protected|public)\s+(?:static\s+)?(?:final\s+)?[\w][\w<>\[\],.\s]*?'
    r'\s+([a-z]\w*)\s*(?:=\s*([^;]+))?;'
)


def _kebab(name):
    """camelCase -> kebab-case, for Spring relaxed binding (``queueName`` -> ``queue-name``)."""
    return re.sub(r'(?<!^)(?=[A-Z])', '-', name).lower()


def _preceding_annotations(text, pos):
    """The text between the previous top-level ``}``/``;`` and ``pos`` — the annotation/modifier run
    immediately above a class or field declaration, without a real parser."""
    prev_end = max(text.rfind("}", 0, pos), text.rfind(";", 0, pos))
    return text[prev_end + 1:pos]


class RepoIndex:
    """Per-repo symbol tables used to resolve a send's destination expression."""
    __slots__ = ("constants", "properties", "value_fields", "getters")

    def __init__(self):
        self.constants = {}     # NAME -> literal
        self.properties = {}    # dotted.key -> literal (from yaml / .properties)
        self.value_fields = {}  # fieldName -> resolved literal ("" if the @Value key isn't in yaml)
        self.getters = {}       # getterName -> resolved literal


def _flatten_yml(text):
    """Best-effort dotted-key -> scalar map from spring-style yaml (no PyYAML; stdlib only)."""
    props, stack = {}, []  # stack items: (indent, key)
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        match = _YML_KV_RE.match(line)
        if not match:
            continue
        indent, key, val = len(match.group(1)), match.group(2), match.group(3).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = [k for _, k in stack] + [key]
        if val and val not in ("|", ">"):
            props[".".join(path)] = val.strip('"\'')
        else:
            stack.append((indent, key))
    return props


def _repo_properties(root):
    """Merged config values from every .yml/.yaml/.properties under the repo (first wins)."""
    props = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for name in filenames:
            low = name.lower()
            if not (low.endswith(_YML_EXT) or low.endswith(".properties")):
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if low.endswith(".properties"):
                for line in text.splitlines():
                    kv = _PROP_LINE_RE.match(line.split("#", 1)[0])
                    if kv:
                        props.setdefault(kv.group(1), kv.group(2).strip('"\''))
            else:
                for key, val in _flatten_yml(text).items():
                    props.setdefault(key, val)
    return props


def _resolve_key(properties, key):
    """A property key -> its literal: exact match, else a UNIQUE last-segment match (spring keys are
    often referenced by a relaxed path); ambiguous or missing -> ''."""
    if key in properties:
        return properties[key]
    tail = key.rsplit(".", 1)[-1]
    hits = {v for k, v in properties.items() if k.rsplit(".", 1)[-1] == tail}
    return next(iter(hits)) if len(hits) == 1 else ""


def _resolve_expr(expr, index):
    """Resolve a simple Java expression to a destination literal via the repo index, else ''."""
    expr = expr.strip()
    literal = re.match(r'^"([^"]+)"$', expr)
    if literal:
        return literal.group(1)
    const = _CONST_REF_RE.search(expr)
    if const and const.group(1) in index.constants:
        return index.constants[const.group(1)]
    ident = _IDENT_RE.match(expr)
    if ident and index.value_fields.get(ident.group(1)):
        return index.value_fields[ident.group(1)]
    getter = _GETTER_CALL_RE.search(expr)
    if getter and index.getters.get(getter.group(1)):
        return index.getters[getter.group(1)]
    return ""


def _build_index(java_texts, properties):
    """Per-repo resolution tables. Order matters: constants + @Value fields first, then getters
    (their bodies may return a constant/field), with a second pass for getters that return getters."""
    index = RepoIndex()
    index.properties = properties
    blob = "\n".join(java_texts)
    for name, lit in _CONST_DEF_RE.findall(blob):
        index.constants.setdefault(name, lit)
    for key, field in _VALUE_FIELD_RE.findall(blob):
        index.value_fields.setdefault(field, _resolve_key(properties, key))
    getter_returns = []
    for text in java_texts:
        for match in _GETTER_DEF_RE.finditer(text):
            body, depth, cut = text[match.end():], 1, None
            for i, ch in enumerate(body[:600]):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        cut = i
                        break
            ret = _RETURN_RE.search(body[:cut] if cut is not None else body[:600])
            if ret:
                getter_returns.append((match.group(1), ret.group(1).strip()))
    # Lombok @Getter/@Data classes/fields and @ConfigurationProperties-bound fields have no getter
    # body to read (generated at build time) — synthesize the getter -> field mapping from the field
    # declaration instead. A field's "return expression" is its initializer if it has one, else its
    # own name (resolved like any other identifier: an @Value field, or a @ConfigurationProperties
    # field bound below via relaxed camelCase -> kebab-case).
    for text in java_texts:
        # Class/field enumeration + brace-matching is real work; skip it outright for the (vast
        # majority of) files that never mention any of these three annotations at all — a single
        # linear scan is far cheaper than doing it per class/field for nothing. This was the main
        # cause of the RUNBOOK-42 Part 8 runtime regression (49s -> 127s on the real mirror).
        if not (_LOMBOK_GETTER_RE.search(text) or _CONFIG_PROPS_RE.search(text)):
            continue
        for cmatch in _CLASS_DECL_RE.finditer(text):
            body_start = cmatch.end() - 1
            depth, i = 1, body_start + 1
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue  # unterminated class body on this pass -> skip rather than guess
            body = text[body_start + 1:i - 1]
            class_has_getter = bool(_LOMBOK_GETTER_RE.search(_preceding_annotations(text, cmatch.start())))
            prefix_match = _CONFIG_PROPS_RE.search(_preceding_annotations(text, cmatch.start()))
            config_prefix = prefix_match.group(1) if prefix_match else ""
            for fmatch in _FIELD_DEF_RE.finditer(body):
                name, init = fmatch.group(1), fmatch.group(2)
                if config_prefix:
                    index.value_fields.setdefault(
                        name, _resolve_key(properties, f"{config_prefix}.{_kebab(name)}"))
                field_has_getter = bool(_LOMBOK_GETTER_RE.search(_preceding_annotations(body, fmatch.start())))
                if not (class_has_getter or field_has_getter):
                    continue
                getter_name = "get" + name[0].upper() + name[1:]
                getter_returns.append((getter_name, init.strip() if init else name))
    for _ in range(2):  # fixpoint: a getter may return the value of another getter
        for method, expr in getter_returns:
            if method not in index.getters:
                resolved = _resolve_expr(expr, index)
                if resolved:
                    index.getters[method] = resolved
    return index


def _first_arg(after_paren):
    """The first top-level argument substring after a call's opening paren."""
    depth, out = 0, []
    for ch in after_paren:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        out.append(ch)
    return "".join(out).strip()


def _all_args(after_paren):
    """All top-level argument substrings after a call's opening paren (mirrors ``_first_arg``)."""
    depth, current, out = 0, [], []
    for ch in after_paren:
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    out.append("".join(current).strip())
    return out


def _looks_like_declaration(line, paren_pos):
    """True if the ``(`` at ``paren_pos`` closes into ``{`` (a method declaration, e.g. a wrapper's
    own ``publishMessage(...)  {``) rather than into ``;``/an operator (a call). The real-mirror
    re-verify found trusted method NAMES (``publishMessage`` etc.) being redeclared as a wrapper's own
    method were being counted as call sites, since the confirmed-receiver guard only applies to the
    generic ``send``/``publish`` names. Multi-line signatures (paren unterminated on this line) are
    left alone rather than guessed at."""
    depth, i, n = 1, paren_pos + 1, len(line)
    while i < n and depth > 0:
        if line[i] == "(":
            depth += 1
        elif line[i] == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return False
    rest = line[i:].lstrip()
    if rest.startswith("throws"):
        rest = re.sub(r'^throws\s+[\w.,\s<>]+', '', rest).lstrip()
    return rest.startswith("{")


def _line_literal(line):
    """The call-site-wide literal fallback (an HRN/queue name adjacent to, not inside, the arg that
    names it). Computed ONCE per call site by the caller — it doesn't depend on which argument is
    being tried, and re-running it per candidate arg was pure waste (RUNBOOK-42 Part 8's runtime
    regression: `line` is typically much longer than any single `arg`)."""
    for regex in (HRN_RE, QUEUE_APP_RE, QUEUE_MQ_RE):
        found = regex.search(line)
        if found:
            name = found.group(0).strip().strip('".,;')
            if len(name) >= 6:
                return name, _kind(name), "literal", "high", name
    return None


def _resolve_destination(arg, line_literal, text, index):
    """Resolve a send's destination expression -> (destination, kind, routing_source, confidence,
    expr). Ladder: literal -> constant (in-file, else repo-wide) -> getter (resolved via the repo
    index) -> @Value field (resolved via yaml) -> unresolved. Unresolved is still a real producer
    signal and is KEPT as a candidate — a getter/@Value we recognise but can't value stays a
    ``builder``/``config`` candidate (better than ``runtime-unresolved``)."""
    for regex in (HRN_RE, QUEUE_APP_RE, QUEUE_MQ_RE):
        found = regex.search(arg)
        if found:
            name = found.group(0).strip().strip('".,;')
            if len(name) >= 6:
                return name, _kind(name), "literal", "high", name
    if line_literal:
        return line_literal
    const = _CONST_REF_RE.search(arg)
    if const:
        name = const.group(1)
        value = re.search(r"\b" + re.escape(name) + r'\s*=\s*"([^"]+)"', text)
        lit = value.group(1) if value else index.constants.get(name, "")
        if lit:
            return lit, _kind(lit), "constant", "high", name
    getter = _GETTER_CALL_RE.search(arg)
    if getter:
        lit = index.getters.get(getter.group(1), "")
        if lit:
            return lit, _kind(lit), "builder", "medium", getter.group(0)
        return "", "", "builder", "medium", getter.group(0)  # getter recognised, value not in repo
    ident = _IDENT_RE.match(arg)
    if ident and ident.group(1) in index.value_fields:
        field = ident.group(1)
        lit = index.value_fields[field]
        if lit:
            return lit, _kind(lit), "config", "medium", field
        return "", "", "config", "low", field  # @Value-bound but the key isn't in this repo's yaml
    return "", "", "runtime-unresolved", "low", ""


def _send_records(repo, rel, lineno, line, text, framework_vars, current_class, index):
    records = []
    for match in _CALL_RE.finditer(line):
        receiver, method = match.group(1), match.group(2)
        if method not in SEND_METHODS:
            continue
        if receiver is None and _looks_like_declaration(line, match.end() - 1):
            continue  # a wrapper's own method DECLARATION (e.g. `void publishMessage(...) {`), not a call
        confirmed = bool(receiver) and receiver in framework_vars
        if method in ("send", "publish") and not confirmed:
            continue  # generic send/publish with an unknown receiver -> too noisy to trust
        # The destination isn't always the first argument (e.g. `send(payload, eventConfig)`) — try
        # each top-level arg in call order (capped at 3: recon never saw a destination past arg 1,
        # and this bounds cost for wide builder calls) and take the first one that actually
        # resolves, falling back to a recognised-but-unresolved candidate over a runtime-unresolved
        # guess. The line-wide literal fallback is computed once, not per candidate arg.
        line_literal = _line_literal(line)
        best = None
        for candidate in _all_args(line[match.end():])[:3]:
            result = _resolve_destination(candidate, line_literal, text, index)
            if result[0]:
                best = result
                break
            if best is None or (best[2] == "runtime-unresolved" and result[2] != "runtime-unresolved"):
                best = result
        dest, kind, source, base_conf, expr = best
        # Base confidence tracks how well the DESTINATION resolved (literal/constant high, builder/
        # config medium, unresolved low). A confirmed messaging receiver lifts it to high (it's
        # certainly a producer); a trusted producer method lifts an otherwise-unresolved send to
        # medium. Neither should DOWNGRADE a strong resolution.
        confidence = base_conf
        if confirmed:
            confidence = "high"
        elif method in _TRUSTED_METHODS and confidence == "low":
            confidence = "medium"
        records.append({
            "producer_repo": repo,
            "destination": dest,
            "consumer_repo": "",
            "routing_source": source,
            "evidence": f"{repo}/{rel}:{lineno}",
            "producer_type": (f"{receiver}.{method}" if receiver else method),
            "producer_symbol": current_class,
            "call_site": f"{repo}/{rel}:{lineno}",
            "destination_expression": expr,
            "destination_kind": kind,
            "confidence": confidence,
            "resolution_status": "resolved" if dest else "unresolved",
        })
    return records


def _wrapper_record(repo, rel, lineno, name, base, confidence):
    return {
        "producer_repo": repo,
        "destination": "",
        "consumer_repo": "",
        "routing_source": "wrapper",
        "evidence": f"{repo}/{rel}:{lineno}",
        "producer_type": f"wrapper:{base}" if base else "wrapper:name-family",
        "producer_symbol": name,
        "call_site": f"{repo}/{rel}:{lineno}",
        "destination_expression": base or name,
        "destination_kind": "",
        "confidence": confidence,
        "resolution_status": "unresolved",
    }


def scan_repo(repo, root):
    """Producer records (wrapper declarations + guarded send sites) for one repo, with destinations
    resolved against a per-repo index (constants, @Value->yaml, getters)."""
    java = []
    for path in _iter_java(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        java.append((rel, lines, "".join(lines)))
    index = _build_index([blob for _, _, blob in java], _repo_properties(root))
    records = []
    for rel, lines, text in java:
        framework_vars = _framework_vars(text)
        current_class = ""
        for lineno, line in enumerate(lines):
            wrapper = _wrapper_base(line)
            if wrapper:
                current_class = wrapper[0]
                records.append(_wrapper_record(repo, rel, lineno + 1, *wrapper))
            records.extend(
                _send_records(repo, rel, lineno + 1, line, text, framework_vars, current_class, index)
            )
    return records


def scan_producers(mirror):
    """Producer records across every repo in the mirror."""
    records = []
    if os.path.isdir(mirror):
        for name in sorted(os.listdir(mirror)):
            root = os.path.join(mirror, name)
            if os.path.isdir(root) and not name.startswith("."):
                records.extend(scan_repo(name, root))
    return records
