# sdlc-ai-recon

Recon toolkit for bootstrapping an **AI coding assistant** over a large,
multi-repo Java system (~400 repos that together form *one* system).

The goal of the wider effort: an internal AI assistant (running on an on-prem
OpenAI-compatible model) that helps maintain the legacy estate and scaffold new
modules. The hard part at this scale isn't the model — it's **retrieval**:
getting the right code in front of the model across 400 repos. This repo is
step zero — understand the estate and extract the cross-repo dependency graph.

## How to "read" 400 repos that live on GitHub (not locally)

Don't download everything per task. Read in three tiers:

| Goal | Read how much | Method |
|---|---|---|
| Recon / dependency graph (now) | only each repo's `pom.xml` (KB) | **API harvest** — `harvest_poms.sh`, no clone |
| Single-repo coding (pilot) | just that one repo | `git clone` on demand, delete after |
| Whole-estate intelligence (later) | all 400, kept fresh | **central mirror + index**; agent queries the index, not raw repos |

The long-term pattern is *read once into a mirror+index, query the index many
times*. Sourcegraph (self-hosted) packages the mirror+index+query and points at
a GitHub org — worth evaluating before building bespoke.

## Files

**Read-only principle:** the 400 repos are production and are never modified.
Everything here only *reads* them; all generated artifacts (maps, indexes,
reports) live in separate folders (`recon_out/`, `index/`), never inside a repo.

| File | What it does |
|---|---|
| `RUNBOOK.md` | **Step 1 — recon.** Hand to opencode; produces one `RECON-REPORT.md` |
| `RUNBOOK-2-cross-repo-qa.md` | **Step 2 — cross-repo Q&A.** Mirror (read-only) + build an index + answer questions with citations |
| `prompts/qa-system-prompt.md` | Operating instructions for the Q&A assistant |
| `impact.py` | Query the dependency graph: blast radius of changing a repo |
| `RUNBOOK-3-message-map.md` | **Step 3 — message map.** Extract who-publishes/consumes which queue/topic (the async wiring) |
| `group.py` | Auto-derive a business-flow bundle of repos to index together (no tribal knowledge needed) |
| `RETRIEVER.md` + `retriever/` + `cli.py` | **Step 4 — the retrieval/index layer.** One read-only toolset (impact, message routing, use-case routing, code search) the assistant queries |
| `mcp_server.py` | Optional MCP wrapper for the retrieval layer (needs `pip install mcp`) |
| `WEBAPP.md` + `webapp/` | **Step 5 — browser Q&A app.** Chat UI + agent loop over the retrieval layer; model wired in one file (`webapp/llm.py`) |
| `harvest_poms.py` | Pull only `pom.xml` from every repo in an org via the GitHub API (no clone). Cross-platform / Windows-friendly — **use this on Windows** |
| `harvest_poms.sh` | Same as above for bash / Git Bash users |
| `recon_maven_graph.py` | Parse the poms, decide *is this Maven multi-repo + shared libs?*, emit the dependency graph |
| `prompts/recon-opencode-tasks.md` | Qualitative recon prompts for opencode (runtime coupling, platform base, a representative slice) |

## Quickstart

```bash
# 1) Harvest poms (replace <ORG> with the GitHub org/owner of the 400 repos)
python harvest_poms.py <ORG> ./poms

# 2) Build the verdict + dependency graph
python recon_maven_graph.py ./poms

# 3) Read the result
#    Windows PowerShell:  Get-Content recon_out/summary.txt
cat recon_out/summary.txt
```

**Internal / enterprise GitHub:** the repos are reached through whatever host
`gh` is logged into. If they live on an internal GitHub Enterprise host, first
`gh auth login --hostname <host>` and set `GH_HOST` (PowerShell:
`$env:GH_HOST="<host>"`) before harvesting.

For the full guided flow on the internal machine, follow **`RUNBOOK.md`**.

Don't have all repos? Run on any subset — the verdict and top shared libs show
up immediately; only the full graph needs the full set.

### Outputs (`recon_out/`)

- `summary.txt` — verdict + stats + top shared libraries + hub repos
- `internal_edges.csv` — `from_repo, to_repo, via_artifact` (the dependency graph)
- `top_shared.csv` — internal artifacts ranked by number of dependent repos
- `produced.csv` — which artifact each repo publishes

## Then what

`recon_out/summary.txt` + `top_shared.csv` + the three opencode outputs are
enough to pin down the P0 pilot scope (pick a domain off the dependency-graph
hubs), the first retrieval-layer component to build, and a repo-guide template.
