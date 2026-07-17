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
3. **A destination resolution ladder** — literal -> constant -> config/@Value -> builder/getter ->
   runtime-unresolved. Unresolved destinations are KEPT (not dropped), tagged so a human/CodeGraph
   pass can finish them.

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


def _resolve_destination(line, text):
    """Resolve a send line's destination -> (destination, kind, routing_source, confidence, expr).

    Ladder (recon §4.3): literal -> constant (value defined in-file) -> builder/getter -> config
    (@Value field) -> runtime-unresolved. The unresolved case is still a real producer signal."""
    for regex in (HRN_RE, QUEUE_APP_RE, QUEUE_MQ_RE):
        found = regex.search(line)
        if found:
            name = found.group(0).strip().strip('".,;')
            if len(name) >= 6:
                return name, _kind(name), "literal", "high", name
    for const in re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", line):
        value = re.search(r"\b" + re.escape(const) + r'\s*=\s*"([^"]+)"', text)
        if value:
            name = value.group(1)
            return name, _kind(name), "constant", "high", const
    getter = re.search(r"\bget(?:Topic\w*|Queue\w*|Destination\w*)\s*\(", line)
    if getter:
        return "", "", "builder", "medium", getter.group(0)
    field = re.search(r"\b([a-z]\w*)\s*\)\s*;?\s*$", line)  # a lone trailing identifier arg
    if field:
        name = field.group(1)
        value = re.search(
            r'@Value\(\s*"(\$\{[^"]+\})"\s*\)[^;]*\b' + re.escape(name) + r"\b", text
        )
        if value:
            return value.group(1), "", "config", "medium", name
    return "", "", "runtime-unresolved", "low", ""


def _send_records(repo, rel, lineno, line, text, framework_vars, current_class):
    records = []
    for match in _CALL_RE.finditer(line):
        receiver, method = match.group(1), match.group(2)
        if method not in SEND_METHODS:
            continue
        confirmed = bool(receiver) and receiver in framework_vars
        if method in ("send", "publish") and not confirmed:
            continue  # generic send/publish with an unknown receiver -> too noisy to trust
        dest, kind, source, base_conf, expr = _resolve_destination(line, text)
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
    """Producer records (wrapper declarations + guarded send sites) for one repo."""
    records = []
    for path in _iter_java(root):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        text = "".join(lines)
        framework_vars = _framework_vars(text)
        current_class = ""
        for index, line in enumerate(lines):
            wrapper = _wrapper_base(line)
            if wrapper:
                current_class = wrapper[0]
                records.append(_wrapper_record(repo, rel, index + 1, *wrapper))
            records.extend(
                _send_records(repo, rel, index + 1, line, text, framework_vars, current_class)
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
