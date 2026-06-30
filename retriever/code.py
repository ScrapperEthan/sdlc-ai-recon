"""Code search/read over the read-only mirror. Uses ripgrep if present, else
falls back to a stdlib walk so it runs with nothing installed."""
import os
import re
import shutil
import fnmatch
import subprocess
from . import config

_SKIP = {'.git', 'target', 'build', 'node_modules', '.codegraph'}


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
    """Return line-numbered source. relpath is relative to mirror/ (or absolute)."""
    path = relpath if os.path.isabs(relpath) else os.path.join(config.MIRROR, relpath)
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    start = max(1, start)
    end = min(len(lines), end or len(lines))
    return "".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
