"""Load and filter generated repo tags."""
import json

from . import config


def _resolve(path):
    if path == "index/repo_tags.json":
        return config.REPO_TAGS_JSON
    return path


def _coerce_entry(entry):
    if not isinstance(entry, dict):
        return {"system": "", "channel": [], "mode": "", "tokens": [], "bundle": ""}
    return {
        "system": (entry.get("system") or "").strip(),
        "channel": [str(item).strip() for item in (entry.get("channel") or []) if str(item).strip()],
        "mode": (entry.get("mode") or "").strip(),
        "tokens": [str(item).strip() for item in (entry.get("tokens") or []) if str(item).strip()],
        "bundle": (entry.get("bundle") or "").strip(),
    }


def load(path="index/repo_tags.json", missing_ok=True):
    try:
        with open(_resolve(path), encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise FileNotFoundError("no repo_tags.json")
    except OSError:
        if missing_ok:
            return {}
        raise FileNotFoundError("no repo_tags.json")

    if not isinstance(payload, dict):
        raise ValueError("repo_tags.json must contain an object")
    return {str(repo): _coerce_entry(entry) for repo, entry in payload.items()}


def for_repo(repo, tags=None):
    payload = load() if tags is None else tags
    return _coerce_entry(payload.get(repo) or {})


def channels_for_repo(repo, tags=None):
    return list(for_repo(repo, tags).get("channel") or [])


def filter_repos(channel=None, mode=None, system=None, bundle=None, path="index/repo_tags.json"):
    payload = load(path=path, missing_ok=False)
    want_channel = (channel or "").strip().lower()
    want_mode = (mode or "").strip().lower()
    want_system = (system or "").strip().lower()
    want_bundle = (bundle or "").strip().lower()

    repos = []
    for repo, meta in sorted(payload.items()):
        channels = {item.lower() for item in meta.get("channel") or []}
        if want_channel and want_channel not in channels:
            continue
        if want_mode and meta.get("mode", "").lower() != want_mode:
            continue
        if want_system and meta.get("system", "").lower() != want_system:
            continue
        if want_bundle and meta.get("bundle", "").lower() != want_bundle:
            continue
        repos.append(repo)

    return {
        "filters": {
            "channel": channel or "",
            "mode": mode or "",
            "system": system or "",
            "bundle": bundle or "",
        },
        "count": len(repos),
        "repos": repos,
    }
