"""
Retrieval layer for the HASE multi-repo assistant.

Read-only. Standard library only. NO database access, NO credentials — it reads
the static artifacts we already produced:
  - recon_out/internal_edges.csv        (dependency graph)
  - index/message_edges.csv             (async message wiring)
  - index/tbl_event_router_usecase_topic.snapshot.csv  (use-case -> topic; dev/SCT)
  - mirror/                             (read-only code copy)

Call it three ways:
  - `python cli.py <tool> ...`          (no agent, no install needed)
  - opencode / any agent via shell       (it runs cli.py)
  - `python mcp_server.py`               (optional MCP server; needs `pip install mcp`)
"""
