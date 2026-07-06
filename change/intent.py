"""Parse a small natural-language ask into a structured change request."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping

from .add_endpoint import _endpoint_path, _method_name


SUPPORTED_KINDS = frozenset({"add_endpoint"})
_PATH_RE = re.compile(r"(?<!\S)(/[A-Za-z0-9_./{}-]+)")
_KIND_RE = re.compile(r"\b(endpoint|route|mapping|get)\b", re.IGNORECASE)
_TRAILING_HINT_RE = re.compile(
    r"\b(?:to|in|on|for)\s+(?P<hint>.+)$",
    re.IGNORECASE,
)
_DROP_WORDS_RE = re.compile(
    r"\b(?:please|add|create|new|a|an|the|get|endpoint|route|mapping|method)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChangeRequest:
    kind: str
    target_hint: str
    path: str
    method: str | None = None


def _validate_request(request: ChangeRequest) -> ChangeRequest:
    kind = (request.kind or "").strip()
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported change kind: {request.kind}")
    hint = re.sub(r"\s+", " ", (request.target_hint or "").strip(" .,:;\"'")).strip()
    if not hint:
        raise ValueError("target hint is required")
    endpoint = _endpoint_path(request.path)
    method = _method_name(request.method) if request.method else None
    return ChangeRequest(kind=kind, target_hint=hint, path=endpoint, method=method)


def _coerce_request(value) -> ChangeRequest:
    if isinstance(value, ChangeRequest):
        return _validate_request(value)
    if isinstance(value, Mapping):
        return _validate_request(ChangeRequest(**value))
    raise TypeError("parser must return ChangeRequest or a mapping")


def _clean_hint(text: str) -> str:
    text = _DROP_WORDS_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,:;\"'")


def _rule_based_parse(text: str) -> ChangeRequest:
    if not text or not text.strip():
        raise ValueError("intent text is required")
    if not _KIND_RE.search(text):
        raise ValueError("intent must describe an endpoint, route, mapping, or GET change")

    path_match = _PATH_RE.search(text)
    if not path_match:
        raise ValueError("intent must include an endpoint path like /status")
    endpoint = _endpoint_path(path_match.group(1))

    without_path = f"{text[: path_match.start()]} {text[path_match.end():]}"
    hint_match = _TRAILING_HINT_RE.search(without_path)
    hint_source = hint_match.group("hint") if hint_match else without_path
    hint = _clean_hint(hint_source)
    if not hint:
        raise ValueError("intent must include a target hint")

    return ChangeRequest(kind="add_endpoint", target_hint=hint, path=endpoint)


def parse_intent(text: str, parser: Callable[[str], object] | None = None) -> ChangeRequest:
    """Parse an ask into the small Phase-2 contract.

    The default parser is deliberately rule-based and stdlib-only. A later LLM
    parser can be injected as long as it returns the same contract.
    """
    if parser is not None:
        return _coerce_request(parser(text))
    return _validate_request(_rule_based_parse(text))
