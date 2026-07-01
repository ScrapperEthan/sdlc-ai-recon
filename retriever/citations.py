"""Extract and verify source citations against the local read-only mirror."""
import functools
import os
import re

from . import config

_CITE = re.compile(
    r"([\w./\-]+?\.(?:java|xml|ya?ml|properties|kts?|json|sql))(?::(\d+)(?:-\d+)?)?",
    re.IGNORECASE,
)


def extract(text):
    """Return [(ref, path, line|None)] in order, de-duplicated by exact ref."""
    out = []
    seen = set()
    for match in _CITE.finditer(text or ""):
        ref = match.group(0)
        if ref in seen:
            continue
        seen.add(ref)
        out.append((ref, match.group(1), int(match.group(2)) if match.group(2) else None))
    return out


def _mirror_real():
    return os.path.realpath(config.MIRROR)


def _inside_mirror(path):
    try:
        return os.path.commonpath([_mirror_real(), os.path.realpath(path)]) == _mirror_real()
    except ValueError:
        return False


@functools.lru_cache(maxsize=1)
def _basename_index():
    idx = {}
    mirror_real = _mirror_real()
    for dirpath, dirnames, filenames in os.walk(mirror_real):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in (".git", "target", "build", "node_modules", ".codegraph")
        ]
        for name in filenames:
            idx.setdefault(name, []).append(os.path.join(dirpath, name))
    return idx


def _resolve(path):
    candidate = os.path.join(config.MIRROR, *path.split("/"))
    if os.path.isfile(candidate) and _inside_mirror(candidate):
        return candidate

    matches = _basename_index().get(os.path.basename(path), [])
    if len(matches) == 1 and _inside_mirror(matches[0]):
        return matches[0]
    return None


def _line_count(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def verify(text):
    """Return a citation verification report for every cited source reference."""
    results = []
    for ref, path, line in extract(text):
        resolved = _resolve(path)
        if not resolved:
            results.append({"ref": ref, "ok": False, "reason": "not found in mirror"})
            continue

        if line is not None:
            count = _line_count(resolved)
            if line > count:
                results.append({"ref": ref, "ok": False, "reason": f"line {line} > {count}"})
                continue

        results.append({"ref": ref, "ok": True, "reason": ""})

    verified = sum(1 for item in results if item["ok"])
    return {"items": results, "verified": verified, "total": len(results)}
