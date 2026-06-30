# RUNBOOK 2 — stand up a read-only cross-repo Q&A assistant (run with opencode)

Goal: let an engineer ask questions like *"how does an inbound message get from
ingress-api to the tracking jobs?"* or *"if I change EventPayload, what breaks?"*
and get a cited answer across the ~390 repos.

**This is 100% READ-ONLY. Never push, never modify any `hase-mc` repo.** All new
files go in a separate `./index/` folder, never inside a cloned repo.

Environment recap (from RUNBOOK 1): GitHub host `alm-github.systems.uk.hsbc`,
org `hase-mc`, `gh` CLI unavailable — use the same Git Credential Manager + GHE
API method that worked for the pom harvest. Python 3.12 present.

## Step A — Mirror the code locally (read-only copy)

Start with a useful SUBSET, not all 400 (faster, proves value, low disk):

1. From `recon_out/top_shared.csv`, take the top ~15 shared libraries' repos.
2. Add the pilot flow repos: `mc-hk-hase-ingress-api`,
   `mc-hk-hase-api-ingress-core`, `mc-hk-hase-api-starter`,
   `mc-hk-hase-api-parent`, and any `*-tracking-job` / `*-dispatch-job` repos.
3. Shallow-clone each into `./mirror/<repo>` (e.g. `git clone --depth 1 <url> ./mirror/<repo>`),
   using the working GCM auth. Skip archived repos. Do not run builds.

Report: how many repos cloned, total disk used. (Expand to all 390 later once
Q&A quality looks good.)

## Step B — Build the index (in `./index/`, NOT inside any repo)

1. Copy `recon_out/internal_edges.csv`, `top_shared.csv`, `produced.csv` into `./index/`.
2. Generate `./index/REPOMAP.md` — for EACH mirrored repo, append a short entry:
   ```
   ## <repo-name>
   - Purpose: <one sentence, from README + top-level packages>
   - Key entry points: <main class / controller / listener>  (cite path:line)
   - Depends on: <internal repos it depends on, from internal_edges.csv>
   - Depended on by: <repos that depend on it>
   ```
   Keep each entry to ~5 lines. Factual, cited. This is the map that lets the
   assistant jump from a question to the right few repos.

## Step C — Answer questions (the assistant)

Adopt `prompts/qa-system-prompt.md` as your operating instructions. To answer
any question: shortlist repos via `REPOMAP.md` + `internal_edges.csv`, then read
the relevant files under `./mirror/`, then answer with `repo/path:line` citations.

Helper for impact questions:
```
python impact.py <repo> --transitive   # who breaks if <repo> changes
python impact.py --hubs                 # riskiest repos to touch
```

## Step D — Evaluate (this is the deliverable to send back)

Answer these starter questions and save each Q + answer to `./index/qa-eval.md`:

1. How does an inbound message flow from `mc-hk-hase-ingress-api` through to the
   tracking jobs? Give the call/event path with citations.
2. If `EventPayload` in `mc-hk-hase-api-ingress-core` changes, which repos are
   affected? (Use `impact.py`.)
3. Where are the HSBC request headers validated, and what are they?
4. Which repos consume the `otxBatchLetter` queue, and who produces to it?
5. To add a new inbound message format, what files do I create/change, and where?

For each: did the assistant find the right repos? Were citations correct? Note
any wrong/missed answers — that tells us where the index needs work.

## Send back (photos or text extracts)

- `./index/REPOMAP.md` (a few entries) and `./index/qa-eval.md` (all 5 answers).
- The cloned-repo count + disk used.
- Anything that blocked you.

These let us judge retrieval quality and decide whether to (a) expand to all 390
and (b) wrap it as a standalone service.
