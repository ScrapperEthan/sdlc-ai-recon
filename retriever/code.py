"""Code search/read over the read-only mirror. Uses ripgrep if present, else
falls back to a stdlib walk so it runs with nothing installed."""
import os
import re
import shutil
import fnmatch
import functools
import subprocess
from . import config

_SKIP = {'.git', 'target', 'build', 'node_modules', '.codegraph'}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _mirror_real():
    return os.path.realpath(config.MIRROR)


def _inside_mirror(path):
    try:
        return os.path.commonpath([_mirror_real(), os.path.realpath(path)]) == _mirror_real()
    except ValueError:
        return False


def _clean_parts(relpath):
    rel = (relpath or "").replace("\\", "/").strip()
    if not rel:
        raise FileNotFoundError(relpath)
    if os.path.isabs(rel) or _DRIVE_RE.match(rel):
        raise ValueError("path escapes mirror")
    parts = [part for part in rel.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ValueError("path escapes mirror")
    return parts


@functools.lru_cache(maxsize=1)
def _basename_index():
    idx = {}
    mirror_real = _mirror_real()
    for dirpath, dirnames, filenames in os.walk(mirror_real):
        dirnames[:] = [name for name in dirnames if name not in _SKIP]
        for name in filenames:
            idx.setdefault(name, []).append(os.path.join(dirpath, name))
    return idx


def _resolve_inside_mirror(relpath):
    raw = (relpath or "").strip()
    if not raw:
        raise FileNotFoundError(relpath)

    if os.path.isabs(raw) or _DRIVE_RE.match(raw):
        full = os.path.realpath(raw)
        if not _inside_mirror(full):
            raise ValueError("path escapes mirror")
        if os.path.isfile(full):
            return full
        raise FileNotFoundError(relpath)

    parts = _clean_parts(raw)
    full = os.path.realpath(os.path.join(config.MIRROR, *parts))
    if not _inside_mirror(full):
        raise ValueError("path escapes mirror")
    if os.path.isfile(full):
        return full

    matches = _basename_index().get(os.path.basename(parts[-1]), [])
    matches = [match for match in matches if _inside_mirror(match)]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(relpath)


def search_code(pattern, glob="*.java", max_results=50):
    """Return matching lines as 'path:line:text'."""
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "-n", "--no-heading", "-S"]
        if glob:
            cmd += ["-g", glob]
        cmd += [pattern, config.MIRROR]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding='utf-8', errors='replace').stdout
            return out.splitlines()[:max_results]
        except Exception:
            pass  # fall through to stdlib

    rx = re.compile(pattern)
    results = []
    for dp, dn, fn in os.walk(config.MIRROR):
        dn[:] = [d for d in dn if d not in _SKIP]
        for name in fn:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            path = os.path.join(dp, name)
            try:
                with open(path, encoding='utf-8', errors='replace') as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            results.append(f"{path}:{i}:{line.rstrip()}")
                            if len(results) >= max_results:
                                return results
            except (OSError, UnicodeError):
                continue
    return results


def read_file(relpath, start=1, end=None):
    """Return line-numbered source from a file inside the mirror."""
    path = _resolve_inside_mirror(relpath)
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    start = max(1, start)
    end = min(len(lines), end or len(lines))
    return "".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))


def read_window(relpath, line=None, ctx=40):
    """Return a structured source window for a file inside mirror/."""
    full = _resolve_inside_mirror(relpath)
    with open(full, encoding="utf-8", errors="replace") as handle:
        all_lines = handle.readlines()

    total = len(all_lines)
    target = int(line) if line else None
    context = max(0, int(ctx))
    if target:
        start = max(1, target - context)
        end = min(total, target + context)
    else:
        start = 1
        end = min(total, 2 * context + 1)

    mirror_real = _mirror_real()
    rel = os.path.relpath(full, mirror_real).replace(os.sep, "/")
    return {
        "path": rel,
        "total": total,
        "start": start,
        "end": end,
        "line": target,
        "lines": [
            {"n": i, "text": all_lines[i - 1].rstrip("\n")}
            for i in range(start, end + 1)
        ],
    }
