#!/usr/bin/env python3
"""
Optional MCP server exposing the retrieval layer as tools, so any MCP-capable
agent (opencode, Copilot, a custom agent) can call them.

Needs the MCP SDK:  pip install mcp
If pip is blocked on the internal network, SKIP this file — opencode can call
cli.py via shell instead (same capabilities, zero install).

Run:  python mcp_server.py
"""
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    raise SystemExit(
        "MCP SDK not found. Either `pip install mcp`, or skip the server and "
        "let the agent call cli.py via shell."
    )

from retriever import graph, messages, code, flow, unified_impact

mcp = FastMCP("sdlc-retriever")


@mcp.tool()
def impact(repo: str, transitive: bool = False) -> dict:
    """Dependency blast radius: who depends on `repo`, and what it depends on."""
    return graph.impact(repo, transitive)


@mcp.tool()
def hubs(top: int = 20) -> list:
    """Most depended-on repos — the riskiest to change."""
    return graph.hubs(top)


@mcp.tool()
def consumers(destination: str) -> list:
    """Repos that consume a queue/topic (substring match)."""
    return messages.who_consumes(destination)


@mcp.tool()
def producers(destination: str) -> list:
    """Repos that produce to a queue/topic (substring match)."""
    return messages.who_produces(destination)


@mcp.tool()
def repo_routes(repo: str) -> list:
    """All message edges (produce/consume) touching a repo."""
    return messages.routes_for_repo(repo)


@mcp.tool()
def usecase_route(use_case_id: str = "", topic: str = "") -> dict:
    """use-case -> topic from the dev/SCT routing snapshot (verify vs prod)."""
    return messages.usecase_route(use_case_id or None, topic or None)


@mcp.tool()
def search_code(pattern: str, glob: str = "*.java", max_results: int = 50) -> list:
    """Search the read-only mirror; returns 'path:line:text'."""
    return code.search_code(pattern, glob, max_results)


@mcp.tool()
def read_file(path: str, start: int = 1, end: int = 0) -> str:
    """Read line-numbered source from the mirror (path relative to mirror/)."""
    return code.read_file(path, start, end or None)


@mcp.tool()
def trace(use_case_id: str = "", destination: str = "") -> dict:
    """Stitch use-case/destination across the async wiring; marks partial honestly."""
    return flow.trace(use_case_id or None, destination or None)


@mcp.tool(name="unified_impact")
def unified_impact_query(seed: str, transitive: bool = False, bundle: str = "") -> dict:
    """Cross-repo call graph + blast radius for a repo OR a symbol (class/method/service).

    Returns real callers from the per-bundle CodeGraph index (auto-routed to the right bundle),
    plus dependency and async-message peers. Prefer this over search_code for any call/usage
    relationship. A bare symbol is resolved to the repo that defines it so deps/messages aren't
    empty. Exposed here so MCP-capable agents get the same flagship tool the webapp has.
    """
    return unified_impact.query(seed, transitive, bundle or None)


@mcp.tool()
def call_graph(query: str) -> dict:
    """Raw `codegraph explore <symbol>`, routed to the bundle that defines the symbol."""
    return unified_impact.call_graph(query)


if __name__ == "__main__":
    mcp.run()
