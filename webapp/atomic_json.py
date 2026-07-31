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
import json
import os
import time

# ~0.5s in total. Long enough for a scanner to let go, short enough that a genuinely locked file
# still fails inside one request rather than hanging the server.
_ATTEMPTS = 5
_BACKOFF_SECONDS = 0.05


def replace(temp_path, path, attempts=_ATTEMPTS):
    """`os.replace` with a short retry on the Windows "someone else has it open" error."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(temp_path, path)
            return attempt
        except PermissionError:
            if attempt >= attempts:
                # Never leave a temp file behind: a stray `<store>.1234.tmp` next to a store is
                # exactly the kind of thing someone later mistakes for a backup.
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
                raise
            time.sleep(_BACKOFF_SECONDS * attempt)


def write_json(path, data, indent=2):
    """Write `data` to `path` atomically. The store is never observed half-written."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp_path = "%s.%d.tmp" % (path, os.getpid())
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=indent)
    replace(temp_path, path)
