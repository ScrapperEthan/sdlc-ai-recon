# Retrieval layer (the "index layer")

One place the assistant asks "where/what/who" about the 400-repo system. It
combines everything we built. **Read-only. Standard library only. No database
access, no credentials** - it reads static files a human produced.

## What it answers

| Tool | Question | Source |
|---|---|---|
| `impact` | If I change repo X, who's affected? | `recon_out/internal_edges.csv` |
| `hubs` | Which repos are riskiest to change? | dependency graph |
| `consumers` / `producers` | Who receives / sends on queue/topic X? | `index/message_edges.csv` |
| `repo-routes` | What does repo X send/receive? | message map |
| `usecase` | Which topic does use-case X route to? | `index/tbl_event_router_usecase_topic.snapshot.csv` (dev/SCT) |
| `trace` | Stitch use-case -> topic -> consuming job | snapshot + message map |
| `search` / `read` | Find / read code | `mirror/` |

Synchronous "who calls whom" stays with **CodeGraph** (`codegraph_explore` /
`codegraph_node`). This layer covers the cross-repo + async parts CodeGraph
can't see, and always marks `partial` instead of inventing a hop.

## How to run

```bash
# from the workspace root (where mirror/, recon_out/, index/ live)
python cli.py hubs --top 15
python cli.py impact mc-hk-hase-api-ingress-core --transitive
python cli.py consumers otx_bat_letter
python cli.py trace --use-case-id UC123
python cli.py search "publishIngressEvent"
python retrieval_service.py
```

- **opencode / any agent**: call `cli.py` via shell - no install, works air-gapped.
  Add a line to its instructions: *"For dependency, message-routing, use-case
  routing, and mirror search, call `python cli.py <tool>`; for who-calls-whom use
  CodeGraph; always cite `repo/path:line`, say `partial` when unproven."*
- **HTTP service for Copilot / curl-based skills**: `python retrieval_service.py`
  exposes the same retrieval layer as read-only JSON over HTTP on
  `RETRIEVAL_HOST`/`RETRIEVAL_PORT` (default `127.0.0.1:8848`). This is the
  path for the peer team's `cross-repo-impact` skill.
- **Optional MCP**: `python mcp_server.py` exposes the same tools over MCP (needs
  `pip install mcp`). Skip if pip is blocked - the shell/CLI path is equivalent.

## Inputs (override locations with env vars)

`SDLC_ROOT` (default cwd), `SDLC_MIRROR`, `SDLC_EDGES`, `SDLC_MSG_EDGES`,
`SDLC_USECASE_SNAPSHOT`. Each tool degrades gracefully if a file is absent
(e.g. `usecase` reports the snapshot is missing rather than failing).

## Index partitioning

`make_bundles.py` turns the full dependency graph into reviewable per-domain
CodeGraph bundle plans, writing `index/bundles.json` without cloning or
indexing anything yet. This keeps the estate-wide dependency and message graphs
global while partitioning only the heavy CodeGraph layer.
