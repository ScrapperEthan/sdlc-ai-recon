"""Atomic JSON writes that survive Windows.

`os.replace` is atomic on both POSIX and Windows, but on Windows it ALSO fails — `PermissionError`,
WinError 5 — whenever anything else holds the destination open. On a corporate Windows box that
means an antivirus scanner or the search indexer that opened the file a millisecond ago. The
intranet saw it as an intermittent test failure (RUNBOOK-61 send-back, 2026-07-31: "900 passed /
1 Windows os.replace 偶发失败, 该测试单独运行通过").

An intermittent test is the cheap symptom. The expensive one is the same race in the running app,
where a dropped replace loses a chat session, a route registration, or a retained log entry — and
the owner's standing rule is that persisted state must never be dropped. So this is fixed at the
write, not in the test.

Two changes over the plain temp-file-then-replace pattern:

* **The temp name carries the pid.** A fixed `<store>.tmp` meant two processes writing the same
  store could truncate each other's half-written file and then race the replace. Rare, but the
  failure is a corrupt store rather than a retry.
* **The replace retries briefly.** The blocking handle is transient — a scanner holds the file for
  milliseconds. Retrying is what makes the difference between "the write landed 80ms late" and
  "the write was lost". After the last attempt the error is RAISED, never swallowed: a write that
  truly cannot land has to be visible.
"""
import itertools
import json
import os
import threading
import time

# ~1.8s in total across 8 attempts. The first version allowed 0.5s and the box still hit the race
# once in a full test run (RUNBOOK-63 send-back), so the budget was simply too small for a
# corporate scanner that does a cloud lookup before letting go. Still bounded: a file that is
# genuinely locked fails inside one request rather than hanging the server.
_ATTEMPTS = 8
_BACKOFF_SECONDS = 0.05

# A per-write suffix, not just per-process. Two sequential writes from one process used to reuse
# one temp name, so a scanner still holding the PREVIOUS temp file blocked the next write's open()
# rather than its replace() — a second way to lose the same write.
_SEQUENCE = itertools.count()
_SEQUENCE_LOCK = threading.Lock()


def _next_temp(path):
    with _SEQUENCE_LOCK:
        return "%s.%d.%d.tmp" % (path, os.getpid(), next(_SEQUENCE))


def _retrying(action, attempts=_ATTEMPTS):
    """Run `action`, retrying the Windows "someone else has it open" error.

    `PermissionError` covers both ERROR_ACCESS_DENIED (5) and ERROR_SHARING_VIOLATION (32); any
    other OSError is a real problem (no such directory, disk full) and is raised immediately rather
    than being sat on for two seconds.
    """
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except PermissionError:
            if attempt >= attempts:
                raise
            time.sleep(_BACKOFF_SECONDS * attempt)


def replace(temp_path, path, attempts=_ATTEMPTS):
    """`os.replace` with a retry, then cleanup if it truly cannot land."""
    try:
        return _retrying(lambda: os.replace(temp_path, path), attempts)
    except PermissionError:
        # Never leave a temp file behind: a stray `<store>.1234.0.tmp` next to a store is exactly
        # the kind of thing someone later mistakes for a backup.
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def write_json(path, data, indent=2):
    """Write `data` to `path` atomically. The store is never observed half-written."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp_path = _next_temp(path)
    payload = json.dumps(data, ensure_ascii=False, indent=indent)

    def _write():
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)

    # Serialised BEFORE the file is opened, so a serialisation error cannot leave a truncated temp
    # file behind, and so a retried open never re-runs the encoding.
    _retrying(_write)
    replace(temp_path, path)
