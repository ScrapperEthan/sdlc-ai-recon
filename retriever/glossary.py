"""Glossary helpers for expanding box-local repo naming abbreviations."""
import json
import re

from . import config

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _resolve(path):
    if path == "index/glossary.json":
        return config.GLOSSARY_JSON
    return path


def load(path="index/glossary.json"):
    """Return a flat {token: meaning} map, or {} when the box-local file is absent."""
    try:
        with open(_resolve(path), encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    out = {}
    for key, value in payload.items():
        token = str(key).strip().lower()
        meaning = str(value).strip()
        if token and meaning:
            out[token] = meaning
    return out


def expand(name, path="index/glossary.json"):
    """Annotate known tokens in a repo-ish name; unchanged when nothing expands."""
    text = (name or "").strip()
    if not text:
        return text

    glossary = load(path)
    if not glossary:
        return text

    parts = []
    seen = set()
    for token in _TOKEN_RE.findall(text):
        key = token.lower()
        if key in glossary and key not in seen:
            parts.append(f"{token}={glossary[key]}")
            seen.add(key)

    if not parts:
        return text
    return f"{text} ({', '.join(parts)})"
