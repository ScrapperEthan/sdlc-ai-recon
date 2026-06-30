# Retrieval layer (the "index layer") — v1

One place the assistant asks "where/what/who" about the 400-repo system. It
combines everything we built. **Read-only. Standard library only. No database
access, no credentials** — it reads static files a human produced.

## What it answers

| Tool | Question | Source |
|---|---|---|
| `impact` | If I change repo X, who's affected? | `recon_out/internal_edges.csv` |
| `hubs` | Which repos are riskiest to change? | dependency graph |
| `consumers` / `producers` | Who receives / sends on queue/topic X? | `index/message_edges.csv` |
| `repo-routes` | What does repo X send/receive? | message map |
| `usecase` | Which topic does use-case X route to? | `index/tbl_event_router_usecase_topic.snapshot.csv` (dev/SCT) |
| `trace` | Stitch use-case → topic → consuming job | snapshot + message map |
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
```

- **opencode / any agent**: call `cli.py` via shell — no install, works air-gapped.
  Add a line to its instructions: *"For dependency, message-routing, use-case
  routing, and mirror search, call `python cli.py <tool>`; for who-calls-whom use
  CodeGraph; always cite `repo/path:line`, say `partial` when unproven."*
- **Optional MCP**: `python mcp_server.py` exposes the same tools over MCP (needs
  `pip install mcp`). Skip if pip is blocked — the shell/CLI path is equivalent.

## Inputs (override locations with env vars)

`SDLC_ROOT` (default cwd), `SDLC_MIRROR`, `SDLC_EDGES`, `SDLC_MSG_EDGES`,
`SDLC_USECASE_SNAPSHOT`. Each tool degrades gracefully if a file is absent
(e.g. `usecase` reports the snapshot is missing rather than failing).

## Not in v1 (next)

A thin web/chat app that calls internal GPT-5.5 + these tools, so developers
without opencode can use it from a browser. That needs the GPT-5.5 endpoint
(base URL / auth / context window). The tools here are front-end independent —
that app, opencode, or Copilot all reuse them unchanged.
