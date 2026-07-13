# RUNBOOK 18 (INTERNAL Codex) — clone the full mirror (unblocks all-bundle CodeGraph)

> **Who runs: INTERNAL Codex on the approved machine** (git access to the internal host). Runs
> `clone_mirror.py` (**pull `master` first**). Fills `mirror/` with the ~390 repos so
> `build_codegraph.py` (RUNBOOK-17) can index every bundle. **Writes only under `mirror/`.** Uses the
> machine's git credential helper — no secrets in the command. Don't push — relay the manifest summary.

## Task A — dry run: confirm the URL pattern + count
```
python clone_mirror.py --dry-run
```
It prints the repo count (from `index/repo_tags.json` / `bundles.json`) and the **URL pattern**. The
default is `https://alm-github.systems.uk.hsbc/hase-mc/<repo>.git`. **If the host/org/protocol differ,
set them** — e.g. ssh:
```
python clone_mirror.py --dry-run --url-template "git@alm-github.systems.uk.hsbc:{org}/{repo}.git"
# or:  --base-url https://<host> --org <org>
```
Relay: the total repo count and the exact URL pattern you'll use.

## Task B — prove ONE clone works (before the big loop)
```
python clone_mirror.py --limit 1        # + the same --url-template/--base-url/--org if you changed them
```
Confirm the single clone succeeds (credential helper handles auth). If it fails, fix the URL/auth and
retry — do **not** start the full run until one works.

## Task C — full clone (resumable)
```
python clone_mirror.py                  # + your URL flags
```
Skips already-present repos, clones the rest, writes `index/mirror_clone.json`. **Relay the summary**:
cloned / skipped / failed counts. Retry any failures:
```
python clone_mirror.py --retry-failed index/mirror_clone.json
```

## Task D — then build all bundles (hand back to RUNBOOK-17)
Once `mirror/` is full, re-run the CodeGraph builder (elevated):
```
python build_codegraph.py --dry-run     # coverage should now be ~full per bundle
python build_codegraph.py               # builds all 31 bundles
```
Relay the new `build_codegraph.py` coverage/manifest — that's the "all bundles lit up" milestone.

## Send back (paste this filled in)
```
Task A plan:   [ repo count; the URL pattern used ]
Task B one:    [ single clone OK? if not, the error + the fix ]
Task C full:   [ cloned / skipped / failed; any failed repos ]
Task D build:  [ build_codegraph coverage after full mirror; bundles built / skipped ]
Surprises:     [ ... ]
```

## What this unlocks
A full `mirror/` is the **one gate** left for estate-wide deep code understanding: with it,
`build_codegraph.py` indexes all 31 bundles and the Q&A app answers symbol-level "who calls / trace"
across the whole estate (RUNBOOK-17 routing). Cross-repo dependency + message impact already works today.
