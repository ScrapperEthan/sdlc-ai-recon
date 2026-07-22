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

import impact_report
from retriever import graph, messages, code, flow, unified_impact, usecase_consistency, usecase_master

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
    """use-case <-> topic from the dev/SCT snapshot. Both args = pair verification, NOT a topic's
    other use cases — for that call use_cases_for_topic."""
    return messages.usecase_route(use_case_id or None, topic or None)


@mcp.tool()
def use_cases_for_topic(topic: str, exact: bool = True, limit: int = 50) -> dict:
    """Reverse lookup: given a TOPIC, list every use case routing to it (dev/SCT snapshot), with
    total/truncated + provenance. Use for 'what other use cases share this topic'."""
    return messages.reverse_lookup_use_cases(topic, exact, limit)


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


@mcp.tool()
def source_system_impact(source_system: str, include_inactive: bool = False,
                          offset: int = 0, limit: int = 50) -> dict:
    """Business-upstream blast radius for an upstream system (PEGA/MDC/eAlert/L400/…): which Use
    Cases it feeds, the Round A coverage funnel (configured/expression_ready/entrypoint_traceable/
    catalog_only — stages, never "reaches the customer"), channel chain, upstream/downstream repos,
    and the layered owners to notify on change. Disabled use cases excluded unless
    include_inactive; `items` defaults to the first `limit` (50) members — the funnel counts are
    always the full total, never truncated."""
    try:
        return impact_report.build_report(
            f"source-system:{source_system}",
            include_inactive=include_inactive, offset=offset, limit=limit,
        )
    except (FileNotFoundError, ValueError) as error:
        return {"ok": False, "error": str(error)}


@mcp.tool()
def list_source_systems() -> list:
    """Distinct CANONICALIZED upstream business systems (source_system) — case/format variants
    folded into one entry with raw_variants listed — with use-case/active/inactive counts."""
    return usecase_master.source_systems()


@mcp.tool()
def usecase_impact(use_case_id: str) -> dict:
    """Full detail for ONE use case: identity/business/governance, consent-preflight, declared
    channels + endpoint repos, the rule_text decision expression as a STRUCTURAL tree
    (rule_text_ast — never read as an asserted fallback/parallel order while semantics is
    "unconfirmed"), validation_findings (rule_text vs channel_rule consistency), upstream/
    downstream repos and the channel chain."""
    try:
        return impact_report.build_report(f"use-case:{use_case_id}")
    except (FileNotFoundError, ValueError) as error:
        return {"ok": False, "error": str(error)}


@mcp.tool()
def search_usecases(query: str = "", source_system: str = "", include_inactive: bool = False,
                     channel: str = "", business_category_code: str = "", country: str = "",
                     service_line: str = "", delivery_mode: str = "",
                     offset: int = 0, limit: int = 50) -> dict:
    """Use Case Catalog search across the full master dataset, server-side paginated (never dumps
    the whole table — items defaults to the first 50 matches)."""
    return usecase_master.search_usecases(
        query=query or None, source_system=source_system or None,
        include_inactive=include_inactive, channel=channel or None,
        business_category_code=business_category_code or None, country=country or None,
        service_line=service_line or None, delivery_mode=delivery_mode or None,
        offset=offset, limit=limit,
    )


@mcp.tool()
def usecase_quality_findings(severity: str = "", offset: int = 0, limit: int = 50) -> dict:
    """Data-quality / consistency findings across the whole active Use Case dataset: orphan
    channel_rule/ext rows, active use cases with no channel rule, missing Ext, null priority,
    PUSH+INBOX unconfigured, illegal business_category, plus per-use-case rule_text-vs-channel_rule
    mismatches. Severity-ranked; these are flagged disagreements, not confirmed prod failures."""
    return usecase_consistency.quality_findings(severity=severity or None, offset=offset, limit=limit)


if __name__ == "__main__":
    mcp.run()
