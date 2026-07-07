# RUNBOOK 6 — size & verify a domain partition for scaling CodeGraph 15 → ~390 repos

Goal: we can't hold ~390 repos in ONE CodeGraph index (~150 MB per 15 repos → multiple GB). So we
must split the estate into **domain bundles**, each its own CodeGraph index. This RUNBOOK proposes
a partition **approach** (below) and asks the **internal Codex** to verify it against the REAL repo
set + graphs, returning the numbers we need to pick boundaries together. **No indexes get built
here** (except optionally one, to size it) — this is measurement only.

**Read-only.** Only read `recon_out/`, `index/`, `mirror/`, and run `group.py` (read-only). Do not
modify `mirror/` or build/commit anything.

---

## The proposed approach (what you're verifying)

1. **Two layers, don't conflate them:**
   - **Estate-wide graphs stay WHOLE** — the dependency graph (`recon_out/internal_edges.csv`) and
     the message map (`index/message_edges.csv`) are small CSVs and already cover all repos. They
     answer the **cross-domain** questions (impact / async routing). **We do NOT partition these.**
   - **Only CodeGraph is partitioned** — CodeGraph gives *in-bundle* call graphs (sync calls
     between repos indexed under one root). Splitting it by domain therefore does **not** lose
     cross-domain impact; that still comes from the whole dependency + message graphs.
2. **A bundle = a domain**, built with the existing `group.py` logic: from a seed service, follow
   deps **downstream** (the shared libs it needs — bounded, never explodes through hubs) + message
   peers + same-domain siblings by name (`--name-contains <token>`).
3. **Shared hub libs** (`api-parent`, `api-starter`, `api-common`, `api-domain`, `api-dao`,
   `api-rest-invoker`, `api-exception`, …) get pulled into each bundle as downstream deps — i.e.
   **duplicated** across bundles. That's fine and intended: they're small and it keeps each bundle
   self-contained for call resolution.
4. **Feasibility target:** keep each bundle roughly **≤ ~40–60 repos** so its CodeGraph DB stays
   ≲ ~0.5 GB. If a domain is bigger, split it further; if many are tiny, merge.

We want DATA to decide the real boundaries: how many domains, how big, any orphans.

---

## Step 0 — Total size of the problem
Report the repo universe:
- Count distinct repos in the dependency graph: unique `from_repo` ∪ `to_repo` in
  `recon_out/internal_edges.csv`.
- Count repo dirs actually present under `mirror/` (may be the 15-repo pilot, not all 390 — say
  which). Also `poms/` count if that's the fuller set.
- One line: "dep-graph knows N repos; mirror has M; poms has K."

## Step 1 — Candidate domains from naming
HASE repos are `mc-hk-hase-<...>`. Tokenize the names to surface domain tokens:
- Strip the `mc-hk-hase-` prefix and common suffixes (`-api`, `-core`, `-job`, `-svc`, `-lib`).
- Report the **frequency table of the remaining leading token(s)** (e.g. `ingress`, `tracking`,
  `sms`, `pn`, `whatsapp`, `email`, `papi`, `sapi`, business-line tokens …), highest count first.
- This is our candidate domain list. Paste the top ~20 tokens + counts.

## Step 2 — The hubs (how much gets duplicated)
- `python cli.py hubs --top 20` → the most depended-on repos + their fan-in.
- These are the repos that will be duplicated into many bundles. Confirm the known ones
  (`api-parent`, `api-starter`, `api-common`, `api-domain`, `api-dao`, `api-rest-invoker`,
  `api-exception`) top the list, and report their fan-in counts.

## Step 3 — Try a few bundles (real sizes)
Pick ~4–6 candidate domains: include the known flow (`ingress`, `tracking`) plus the biggest
tokens from Step 1. For each, run `group.py` and report the bundle size:
```
python group.py <a-seed-repo-in-that-domain> --name-contains <domain-token>
```
(Use a real seed from that domain; the `--name-contains` sweeps in the domain siblings, the seed
pulls the shared libs.) Report, per candidate domain:
- token, seed used, **bundle repo count**, and whether it's within the ≤~60 target.
- If a bundle is huge (hub explosion), note it — that means the seed/token is too broad.

## Step 4 — Coverage / orphans / overlap
- **Union**: how many distinct repos do your Step-3 bundles cover together, vs the Step-0 total?
- **Orphans**: list (or count) repos in the dep graph that fall into **none** of the candidate
  bundles — these need their own domain or a catch-all bundle.
- **Overlap**: roughly how many repos appear in **multiple** bundles (expected = the hubs).

## Step 5 (optional, only if quick) — CodeGraph size reality check
For the **largest** Step-3 bundle only: if you can clone/copy those repos under one scratch root and
`codegraph init .` cheaply, report the resulting `.codegraph/` DB size. Otherwise, estimate from
the 150 MB / 15-repo heuristic and say it's an estimate. Do NOT build all bundles.

## Send back (paste this filled in)
```
Step 0 universe:     [ dep-graph N repos / mirror M / poms K ]
Step 1 domains:      [ top ~20 name tokens + counts ]
Step 2 hubs:         [ top hubs + fan-in; do the known 7 lead? ]
Step 3 bundles:      [ per candidate: token, seed, repo count, within ≤~60? ]
Step 4 coverage:     [ union vs total; #orphans (+examples); #overlap repos ]
Step 5 codegraph:    [ largest bundle DB size (measured or estimated) ]
Surprises / errors:  [ ... ]
```

## What we do with this
From the numbers we jointly decide the final domain list (how many bundles, where to split/merge,
how to handle orphans), then wire it into a scaling plan: per-bundle CodeGraph build + a
`bundles.json` the retrieval layer reads, with the estate-wide dep/message graphs staying global.
Nothing is committed off the box; you relay the report and we pick boundaries together.
