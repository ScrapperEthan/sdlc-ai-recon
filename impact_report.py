#!/usr/bin/env python3
"""Compose a cited impact report from the existing retrieval primitives."""
import argparse
import collections
import csv
import os
import re

from retriever import config, flow, glossary, graph, messages, repo_tags, usecase_master

CHANNEL_KEYWORDS = ("sms", "email", "push", "whatsapp", "wechat", "letter")
EDGE_FIELDS = ("producer_repo", "destination", "consumer_repo", "routing_source", "evidence")


def parse_target(raw):
    text = (raw or "").strip()
    if not text:
        raise ValueError("target is required")
    if ":" in text:
        kind, value = text.split(":", 1)
        kind = kind.strip().lower()
        value = value.strip()
        if kind in {"topic", "use-case", "source-system"}:
            if not value:
                raise ValueError("target is required")
            return kind, value
    return "repo", text


def sanitize_target(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "target"


def display_path(path):
    try:
        rel = os.path.relpath(path, config.ROOT)
    except ValueError:
        rel = path
    return rel.replace(os.sep, "/")


def line_citation(path, line_no):
    return f"{display_path(path)}:{line_no}"


def find_string_citation(path, needle):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line_no, line in enumerate(handle, 1):
                if needle in line:
                    return line_citation(path, line_no)
    except OSError:
        return ""
    return ""


def load_edges():
    fwd = collections.defaultdict(set)
    rows = {}
    try:
        with open(config.EDGES_CSV, newline="", encoding="utf-8-sig") as handle:
            for line_no, row in enumerate(csv.DictReader(handle), 2):
                source = (row.get("from_repo") or "").strip()
                target = (row.get("to_repo") or "").strip()
                if source and target:
                    fwd[source].add(target)
                    rows.setdefault((source, target), []).append(line_no)
    except FileNotFoundError:
        pass
    return fwd, rows


def load_message_rows():
    rows = []
    try:
        with open(config.MESSAGE_EDGES_CSV, newline="", encoding="utf-8-sig") as handle:
            for line_no, row in enumerate(csv.DictReader(handle), 2):
                clean = {field: (row.get(field) or "").strip() for field in EDGE_FIELDS}
                rows.append((line_no, clean))
    except FileNotFoundError:
        pass
    return rows


def load_usecase_rows():
    rows = []
    cols = []
    try:
        with open(config.USECASE_SNAPSHOT_CSV, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            cols = reader.fieldnames or []
            for line_no, row in enumerate(reader, 2):
                rows.append((line_no, row))
    except FileNotFoundError:
        pass
    return rows, cols


def detect_column(cols, *needles):
    for col in cols:
        flat = col.lower().replace("_", "")
        if all(needle in flat for needle in needles):
            return col
    return None


def find_path(adj, start, goal):
    if start == goal:
        return [start]
    queue = collections.deque([start])
    parents = {start: None}
    while queue:
        current = queue.popleft()
        for nxt in sorted(adj.get(current, ())):
            if nxt in parents:
                continue
            parents[nxt] = current
            if nxt == goal:
                path = [goal]
                while parents[path[-1]] is not None:
                    path.append(parents[path[-1]])
                path.reverse()
                return path
            queue.append(nxt)
    return []


def path_citations(path, edge_lines):
    cites = []
    for left, right in zip(path, path[1:]):
        for line_no in edge_lines.get((left, right), ()):
            cites.append(line_citation(config.EDGES_CSV, line_no))
    return sorted(set(cites))


def edge_mentions(repo):
    _adj, edge_lines = load_edges()
    cites = []
    for (left, right), line_nos in edge_lines.items():
        if repo in {left, right}:
            for line_no in line_nos:
                cites.append(line_citation(config.EDGES_CSV, line_no))
    return sorted(set(cites))


def route_signature(row):
    return tuple((row.get(field) or "").strip() for field in EDGE_FIELDS)


def route_citations(row, indexed_rows):
    cites = set()
    signature = route_signature(row)
    for line_no, indexed in indexed_rows:
        if route_signature(indexed) == signature:
            cites.add(line_citation(config.MESSAGE_EDGES_CSV, line_no))
    evidence = (row.get("evidence") or "").strip()
    if evidence:
        cites.add(evidence)
    return sorted(cites)


def keyword_channels(text):
    lowered = (text or "").lower()
    return [channel for channel in CHANNEL_KEYWORDS if channel in lowered]


def channel_chain(tags, repos, names, route_rows):
    details = {}

    def ensure(channel):
        return details.setdefault(channel, {"channel": channel, "sources": set(), "citations": set()})

    for repo in sorted(repos):
        tagged_channels = repo_tags.channels_for_repo(repo, tags)
        if tagged_channels:
            repo_citation = find_string_citation(config.REPO_TAGS_JSON, f'"{repo}"')
            for channel in tagged_channels:
                item = ensure(channel)
                item["sources"].add(f"repo tag:{repo}")
                if repo_citation:
                    item["citations"].add(repo_citation)
            continue
        for channel in keyword_channels(repo):
            ensure(channel)["sources"].add(f"repo name:{repo}")

    indexed_rows = load_message_rows()
    for name in names:
        for channel in keyword_channels(name):
            ensure(channel)["sources"].add(f"keyword:{name}")

    for row in route_rows:
        destination = row.get("destination") or ""
        cites = route_citations(row, indexed_rows)
        for channel in keyword_channels(destination):
            item = ensure(channel)
            item["sources"].add(f"destination:{destination}")
            item["citations"].update(cites)

    out = []
    for channel in sorted(details):
        item = details[channel]
        out.append(
            {
                "channel": channel,
                "sources": sorted(item["sources"]),
                "citations": sorted(item["citations"]),
            }
        )
    return out


def glossary_citations(text):
    tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9]+", text or "")}
    cites = []
    for token in sorted(tokens):
        citation = find_string_citation(config.GLOSSARY_JSON, f'"{token}"')
        if citation:
            cites.append(citation)
    return sorted(set(cites))


def route_groups(route_rows, tags):
    indexed_rows = load_message_rows()
    groups = collections.OrderedDict()
    for row in route_rows:
        destination = (row.get("destination") or "").strip()
        if not destination:
            continue
        item = groups.setdefault(
            destination,
            {
                "destination": destination,
                "producers": set(),
                "consumers": set(),
                "routing_sources": set(),
                "citations": set(),
            },
        )
        producer = (row.get("producer_repo") or "").strip()
        consumer = (row.get("consumer_repo") or "").strip()
        if producer:
            item["producers"].add(producer)
        if consumer:
            item["consumers"].add(consumer)
        source = (row.get("routing_source") or "").strip()
        if source:
            item["routing_sources"].add(source)
        item["citations"].update(route_citations(row, indexed_rows))

    out = []
    for destination, item in groups.items():
        repos = set(item["producers"]) | set(item["consumers"])
        channels = channel_chain(tags, repos, [destination], [])
        out.append(
            {
                "destination": destination,
                "producers": sorted(item["producers"]),
                "consumers": sorted(item["consumers"]),
                "routing_sources": sorted(item["routing_sources"]),
                "channels": [entry["channel"] for entry in channels],
                "citations": sorted(item["citations"]),
            }
        )
    return out


def repo_relation_items(repo):
    transitive = graph.impact(repo, transitive=True)
    direct = graph.impact(repo, transitive=False)
    adj, edge_lines = load_edges()

    upstream = []
    for dependency in transitive.get("depends_on") or []:
        path = find_path(adj, repo, dependency)
        upstream.append(
            {
                "repo": dependency,
                "relation": "direct" if dependency in (direct.get("depends_on") or []) else "transitive",
                "path": path,
                "citations": path_citations(path, edge_lines),
            }
        )

    downstream = []
    for dependent in transitive.get("depended_on_by") or []:
        path = find_path(adj, dependent, repo)
        downstream.append(
            {
                "repo": dependent,
                "relation": "direct" if dependent in (direct.get("depended_on_by") or []) else "transitive",
                "path": path,
                "citations": path_citations(path, edge_lines),
            }
        )
    return upstream, downstream


def aggregate_repo_relations(seed_repos, exclude=None):
    excluded = set(exclude or ())
    adj, edge_lines = load_edges()
    upstream = {}
    downstream = {}

    for seed in sorted(set(seed_repos)):
        transitive = graph.impact(seed, transitive=True)
        direct = graph.impact(seed, transitive=False)

        for dependency in transitive.get("depends_on") or []:
            if dependency in excluded:
                continue
            path = find_path(adj, seed, dependency)
            item = upstream.setdefault(
                dependency,
                {"repo": dependency, "relation": "transitive", "via_repos": set(), "path": [], "citations": set()},
            )
            if dependency in (direct.get("depends_on") or []):
                item["relation"] = "direct"
            if not item["path"] or (item["relation"] == "direct" and len(path) < len(item["path"])):
                item["path"] = path
            item["via_repos"].add(seed)
            item["citations"].update(path_citations(path, edge_lines))

        for dependent in transitive.get("depended_on_by") or []:
            if dependent in excluded:
                continue
            path = find_path(adj, dependent, seed)
            item = downstream.setdefault(
                dependent,
                {"repo": dependent, "relation": "transitive", "via_repos": set(), "path": [], "citations": set()},
            )
            if dependent in (direct.get("depended_on_by") or []):
                item["relation"] = "direct"
            if not item["path"] or (item["relation"] == "direct" and len(path) < len(item["path"])):
                item["path"] = path
            item["via_repos"].add(seed)
            item["citations"].update(path_citations(path, edge_lines))

    def finalize(items):
        out = []
        for repo in sorted(items):
            item = items[repo]
            out.append(
                {
                    "repo": item["repo"],
                    "relation": item["relation"],
                    "via_repos": sorted(item["via_repos"]),
                    "path": item["path"],
                    "citations": sorted(item["citations"]),
                }
            )
        return out

    return finalize(upstream), finalize(downstream)


def matched_usecase_citations(use_case_id=None, topic=None):
    rows, cols = load_usecase_rows()
    if not rows:
        return []
    usecase_col = detect_column(cols, "usecase", "id") or detect_column(cols, "usecase")
    topic_col = detect_column(cols, "topic")
    cites = []
    for line_no, row in rows:
        if use_case_id and use_case_id.lower() not in (row.get(usecase_col, "") or "").lower():
            continue
        if topic and topic.lower() not in (row.get(topic_col, "") or "").lower():
            continue
        cites.append(line_citation(config.USECASE_SNAPSHOT_CSV, line_no))
    return cites


def risk_callouts(relevant_repos, upstream, downstream, routes, notes=None, note_citations=None):
    relevant = set(relevant_repos)
    relevant.update(item["repo"] for item in upstream)
    relevant.update(item["repo"] for item in downstream)
    for route in routes:
        relevant.update(route.get("producers") or [])
        relevant.update(route.get("consumers") or [])

    hubs = {item["repo"]: item["dependents"] for item in graph.hubs(max(20, len(graph.known_repos()) or 20))}
    callouts = []
    for repo in sorted(relevant):
        if repo in hubs:
            citations = edge_mentions(repo)
            callouts.append(
                {
                    "type": "hub",
                    "repo": repo,
                    "dependents": hubs[repo],
                    "message": f"{repo} is a dependency hub in this blast path",
                    "citations": citations[:6],
                }
            )

    for note in notes or []:
        callouts.append(
            {
                "type": "honesty",
                "message": note,
                "citations": list(note_citations or []),
            }
        )
    return callouts


def flatten_citations(report):
    cites = set(report["target"].get("citations") or [])
    for section in ("upstream", "downstream", "async_routes", "channel_chain", "risk_callouts"):
        for item in report.get(section) or []:
            cites.update(item.get("citations") or [])
    return sorted(cites)


def repo_exists(repo, tags):
    return repo in graph.known_repos() or bool(messages.routes_for_repo(repo)) or repo in tags


def build_repo_report(repo, tags):
    if not repo_exists(repo, tags):
        raise FileNotFoundError(f"unknown target: {repo}")

    upstream, downstream = repo_relation_items(repo)
    routes = route_groups(messages.routes_for_repo(repo), tags)
    route_rows = messages.routes_for_repo(repo)
    chain = channel_chain(tags, {repo}, [route["destination"] for route in routes], route_rows)
    relevant_repos = {repo}
    for route in routes:
        relevant_repos.update(route["producers"])
        relevant_repos.update(route["consumers"])

    return {
        "target": {
            "input": repo,
            "kind": "repo",
            "value": repo,
            "description": glossary.expand(repo),
            "channels": [item["channel"] for item in chain],
            "citations": glossary_citations(repo),
        },
        "upstream": upstream,
        "downstream": downstream,
        "async_routes": routes,
        "channel_chain": chain,
        "risk_callouts": risk_callouts(relevant_repos, upstream, downstream, routes),
    }


def build_topic_report(topic, tags):
    route_rows = []
    seen = set()
    for row in messages.who_produces(topic) + messages.who_consumes(topic):
        signature = route_signature(row)
        if signature in seen:
            continue
        seen.add(signature)
        route_rows.append(row)
    if not route_rows:
        raise FileNotFoundError(f"unknown target: topic:{topic}")

    routes = route_groups(route_rows, tags)
    participants = set()
    for route in routes:
        participants.update(route["producers"])
        participants.update(route["consumers"])
    upstream, downstream = aggregate_repo_relations(participants, exclude=participants)
    chain = channel_chain(tags, participants, [route["destination"] for route in routes] + [topic], route_rows)

    return {
        "target": {
            "input": f"topic:{topic}",
            "kind": "topic",
            "value": topic,
            "description": topic,
            "channels": [item["channel"] for item in chain],
            "citations": [],
        },
        "upstream": upstream,
        "downstream": downstream,
        "async_routes": routes,
        "channel_chain": chain,
        "risk_callouts": risk_callouts(participants, upstream, downstream, routes),
    }


def build_usecase_report(use_case_id, tags):
    trace = flow.trace(use_case_id=use_case_id)
    usecase = messages.usecase_route(use_case_id=use_case_id)
    if usecase.get("available") and not usecase.get("matches"):
        raise FileNotFoundError(f"unknown target: use-case:{use_case_id}")

    route_rows = []
    topics = []
    if usecase.get("available"):
        for match in usecase.get("matches") or []:
            topic = (match.get("topic") or "").strip()
            if topic:
                topics.append(topic)
                for row in messages.who_produces(topic) + messages.who_consumes(topic):
                    route_rows.append(row)

    deduped = []
    seen = set()
    for row in route_rows:
        signature = route_signature(row)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(row)

    routes = route_groups(deduped, tags)
    participants = set()
    for route in routes:
        participants.update(route["producers"])
        participants.update(route["consumers"])
    upstream, downstream = aggregate_repo_relations(participants, exclude=participants)
    notes = list(trace.get("partial") or [])
    if usecase.get("available"):
        notes.append("use-case routing is from a dev/SCT snapshot and should be verified against prod")
    chain = channel_chain(tags, participants, topics, deduped)

    usecase_cites = matched_usecase_citations(use_case_id=use_case_id)
    master = usecase_master.master_for(use_case_id)
    target = {
        "input": f"use-case:{use_case_id}",
        "kind": "use-case",
        "value": use_case_id,
        "description": use_case_id,
        "channels": [item["channel"] for item in chain],
        "matched_topics": topics,
        "citations": usecase_cites,
    }
    report = {
        "target": target,
        "upstream": upstream,
        "downstream": downstream,
        "async_routes": routes,
        "channel_chain": chain,
        "risk_callouts": risk_callouts(
            participants,
            upstream,
            downstream,
            routes,
            notes=notes,
            note_citations=usecase_cites[:1],
        ),
    }
    # Additive + null-safe: no master row (or no snapshot at all) -> byte-identical to today.
    if master:
        target["description"] = f"{master['use_case_id']} — {master['name']}" if master["name"] else use_case_id
        target["business"] = {
            "source_system": master["source_system"],
            "project": master["project"],
            "work_stream": master["work_stream"],
            "line_of_business": master["line_of_business"],
            "business_category_code": master["business_category_code"],
            "business_category_label": master["business_category_label"],
            "country": master["country"],
            "group_member": master["group_member"],
            "app": master["app"],
            "citation": master["citation"],
        }
        target["governance"] = {
            "created_by": master["created_by"],
            "created_time": master["created_time"],
            "modified_by": master["modified_by"],
            "last_modified_time": master["last_modified_time"],
            "status": master["status"],
            "stale": usecase_master.is_stale(master["last_modified_time"], master["created_time"]),
            "citation": master["citation"],
        }
        consent = usecase_master.consent_preflight(use_case_id)
        report["consent_preflight"] = consent
        extra_cites = {master["citation"]}
        extra_cites.update(check["citation"] for check in consent.get("checks") or [])
        # Round A fact-only channels (tbl_use_case_channel_rule.channel) + declared endpoint repos
        # (tbl_use_case_ext.endpoint) — additive; absent tables leave the report unchanged.
        key = use_case_id.strip().lower()
        rules = usecase_master.rules_by_use_case_id().get(key) or []
        ext = usecase_master.ext_by_use_case_id().get(key)
        if rules:
            target["channels_declared"] = usecase_master.channels_for_use_case(use_case_id)
            report["channel_rules"] = rules
            extra_cites.update(rule["citation"] for rule in rules if rule.get("citation"))
        if ext:
            report["ext"] = ext
            if ext.get("citation"):
                extra_cites.add(ext["citation"])
            endpoint_repos = usecase_master.resolve_endpoint(ext.get("endpoint") or "")
            if endpoint_repos:
                report["endpoint_repos"] = endpoint_repos
        target["citations"] = sorted(set(target["citations"]) | extra_cites)
    return report


def build_source_system_report(value, tags, include_inactive=False, offset=0, limit=None):
    """Aggregate a source_system's members using the Round A coverage funnel — configured
    (>=1 channel rule) / expression_ready (non-blank rule_text) / entrypoint_traceable (endpoint
    resolves to a known repo) / catalog_only (no rule, no ext) — REPLACING the old routed-vs-
    catalog-only split, which computed "traced channel" from the dev/SCT route snapshot even when
    the active dataset is UAT (defect #2). The confidence banner always says which stages a number
    covers; never presents `configured` as "reaches the customer"."""
    members = usecase_master.use_cases_for_source_system(
        value, include_inactive=include_inactive, offset=offset, limit=limit)
    if not members.get("available") or not members.get("items"):
        raise FileNotFoundError(f"unknown target: source-system:{value}")

    items = members["items"]
    coverage = usecase_master.source_system_coverage(value)
    route = usecase_master.route_dimension()

    route_rows = []
    topics = []
    seen_topics = set()
    if route["available"]:
        for item in items:
            if not item.get("has_route"):
                continue
            usecase = messages.usecase_route(use_case_id=item["use_case_id"])
            if not usecase.get("available"):
                continue
            for match in usecase.get("matches") or []:
                topic = (match.get("topic") or "").strip()
                if not topic or topic in seen_topics:
                    continue
                seen_topics.add(topic)
                topics.append(topic)
                route_rows.extend(messages.who_produces(topic) + messages.who_consumes(topic))

    deduped = []
    seen = set()
    for row in route_rows:
        signature = route_signature(row)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(row)

    routes = route_groups(deduped, tags)
    participants = set()
    for route_item in routes:
        participants.update(route_item["producers"])
        participants.update(route_item["consumers"])
    upstream, downstream = aggregate_repo_relations(participants, exclude=participants)
    chain = channel_chain(tags, participants, topics, deduped)
    owners = usecase_master.owners_for(item["use_case_id"] for item in items)

    citations = set(item["citation"] for item in items if item.get("citation"))
    for route_item in routes:
        citations.update(route_item.get("citations") or [])
    for item in upstream + downstream:
        citations.update(item.get("citations") or [])
    for item in chain:
        citations.update(item.get("citations") or [])

    banner = (
        f"渠道规则覆盖 {coverage['configured']}/{coverage['total']} 个 Use Case（configured）；"
        f"其中 {coverage['expression_ready']} 个有可用路由表达式（expression_ready）、"
        f"{coverage['entrypoint_traceable']} 个可追踪到入口 repo（entrypoint_traceable）；"
        f"{coverage['catalog_only']} 个仅有业务登记、无渠道规则也无扩展信息（catalog_only）。"
    )
    if not route["available"]:
        banner += f" 路由维度不可用（{route['reason']}）——以上覆盖来自渠道规则/扩展表，不是路由快照。"

    return {
        "target": {
            "input": f"source-system:{value}",
            "kind": "source-system",
            "value": value,
            "canonical": coverage["canonical"],
            "display_name": coverage["display_name"],
            "use_case_count": coverage["total"],
            "active_count": coverage["active"],
            "inactive_count": coverage["total"] - coverage["active"],
            "coverage": {k: coverage[k] for k in
                         ("configured", "expression_ready", "entrypoint_traceable", "catalog_only")},
            "route_dimension": route,
        },
        "use_cases": {
            "items": items, "total": members["total"], "active_count": members["active_count"],
            "inactive_count": members["inactive_count"], "returned": members["returned"],
            "truncated": members["truncated"],
        },
        "topics": topics,
        "async_routes": routes,
        "channel_chain": chain,
        "upstream": upstream,
        "downstream": downstream,
        "owners": owners,
        "confidence_banner": banner,
        "citations": sorted(citations),
    }


def build_report(target, include_inactive=False, offset=0, limit=None):
    tags = repo_tags.load(missing_ok=True)
    kind, value = parse_target(target)
    if kind == "repo":
        report = build_repo_report(value, tags)
    elif kind == "topic":
        report = build_topic_report(value, tags)
    elif kind == "source-system":
        return build_source_system_report(
            value, tags, include_inactive=include_inactive, offset=offset, limit=limit)
    else:
        report = build_usecase_report(value, tags)
    report["citations"] = flatten_citations(report)
    return report


def render_repo_items(items):
    if not items:
        return ["- none known"]
    lines = []
    for item in items:
        bits = [f"- {item['repo']} [{item['relation']}]"]
        if item.get("via_repos"):
            bits.append(f"via {', '.join(item['via_repos'])}")
        if item.get("path"):
            bits.append("path: " + " -> ".join(item["path"]))
        if item.get("citations"):
            bits.append("citations: " + ", ".join(item["citations"]))
        lines.append(" | ".join(bits))
    return lines


def render_routes(routes):
    if not routes:
        return ["- none known"]
    lines = []
    for route in routes:
        bits = [f"- {route['destination']}"]
        bits.append("producers: " + (", ".join(route["producers"]) if route["producers"] else "none"))
        bits.append("consumers: " + (", ".join(route["consumers"]) if route["consumers"] else "none"))
        if route.get("channels"):
            bits.append("channels: " + ", ".join(route["channels"]))
        if route.get("citations"):
            bits.append("citations: " + ", ".join(route["citations"]))
        lines.append(" | ".join(bits))
    return lines


def render_channel_chain(items):
    if not items:
        return ["- unknown"]
    lines = []
    for item in items:
        bits = [f"- {item['channel']}"]
        if item.get("sources"):
            bits.append("sources: " + ", ".join(item["sources"]))
        if item.get("citations"):
            bits.append("citations: " + ", ".join(item["citations"]))
        lines.append(" | ".join(bits))
    return lines


def render_risk_callouts(items):
    if not items:
        return ["- none"]
    lines = []
    for item in items:
        bits = [f"- {item['type']}"]
        if item.get("repo"):
            bits.append(item["repo"])
        if item.get("dependents") is not None:
            bits.append(f"dependents={item['dependents']}")
        if item.get("message"):
            bits.append(item["message"])
        if item.get("citations"):
            bits.append("citations: " + ", ".join(item["citations"]))
        lines.append(" | ".join(bits))
    return lines


def render_markdown(report):
    target = report["target"]
    lines = [
        f"# Impact Report — {target['input']}",
        "",
        "## Target",
        f"- Asked: {target['kind']} `{target['value']}`",
        f"- Description: {target['description']}",
        f"- Channels: {', '.join(target['channels']) if target['channels'] else 'unknown'}",
    ]
    if target.get("matched_topics"):
        lines.append("- Matched topics: " + ", ".join(target["matched_topics"]))
    if target.get("citations"):
        lines.append("- Citations: " + ", ".join(target["citations"]))

    lines.extend(["", "## Upstream (what it depends on)"])
    lines.extend(render_repo_items(report["upstream"]))
    lines.extend(["", "## Downstream (who's affected)"])
    lines.extend(render_repo_items(report["downstream"]))
    lines.extend(["", "## Async Routes"])
    lines.extend(render_routes(report["async_routes"]))
    lines.extend(["", "## Channel Chain"])
    lines.extend(render_channel_chain(report["channel_chain"]))
    lines.extend(["", "## Risk Callouts"])
    lines.extend(render_risk_callouts(report["risk_callouts"]))
    lines.extend(["", "## Citations"])
    lines.extend([f"- {citation}" for citation in report.get("citations") or []] or ["- none"])
    return "\n".join(lines).rstrip() + "\n"


def render_source_system_markdown(report):
    target = report["target"]
    coverage = target["coverage"]
    lines = [
        f"# Impact Report — {target['input']}",
        "",
        "## Target",
        f"- source_system: `{target['value']}` (canonical: `{target['canonical']}`, "
        f"display: {target['display_name']})",
        f"- Use cases: {target['use_case_count']} total, {target['active_count']} active, "
        f"{target['inactive_count']} inactive",
        f"- Coverage funnel: configured {coverage['configured']}, "
        f"expression_ready {coverage['expression_ready']}, "
        f"entrypoint_traceable {coverage['entrypoint_traceable']}, "
        f"catalog_only {coverage['catalog_only']}",
        f"- Route dimension: {'available' if target['route_dimension']['available'] else 'unavailable'}"
        + (f" ({target['route_dimension']['reason']})" if target['route_dimension'].get('reason') else ""),
        "",
        "## Confidence",
        report["confidence_banner"],
        "",
        f"## Use cases ({report['use_cases']['returned']}/{report['use_cases']['total']}"
        + (", truncated" if report['use_cases']['truncated'] else "") + ")",
    ]
    lines.extend([
        f"- `{item['use_case_id']}` — {item['name'] or 'unnamed'} ({item['project'] or 'no project'}); "
        f"active={item['active']} configured={item['configured']} "
        f"expression_ready={item['expression_ready']} entrypoint_traceable={item['entrypoint_traceable']}; "
        f"citation: {item['citation']}"
        for item in report["use_cases"]["items"]
    ] or ["- none"])
    lines.extend(["", "## Topics"])
    lines.extend([f"- {topic}" for topic in report["topics"]] or ["- none"])
    lines.extend(["", "## Async Routes"])
    lines.extend(render_routes(report["async_routes"]))
    lines.extend(["", "## Channel Chain"])
    lines.extend(render_channel_chain(report["channel_chain"]))
    lines.extend(["", "## Upstream"])
    lines.extend(render_repo_items(report["upstream"]))
    lines.extend(["", "## Downstream"])
    lines.extend(render_repo_items(report["downstream"]))
    owners = report["owners"]
    lines.extend(["", f"## Owners ({sum(len(v) for v in owners.values())})"])
    for group_key, group_label in (
        ("business_owners", "Business owners"),
        ("cost_governance", "Cost / governance sign-off"),
        ("config_maintainers", "Config maintainers (created_by/modified_by)"),
    ):
        lines.append(f"### {group_label}")
        lines.extend([f"- {name}" for name in owners.get(group_key) or []] or ["- none"])
    lines.extend(["", "## Citations"])
    lines.extend([f"- {citation}" for citation in report.get("citations") or []] or ["- none"])
    return "\n".join(lines).rstrip() + "\n"


def render_report_markdown(report):
    if report.get("target", {}).get("kind") == "source-system":
        return render_source_system_markdown(report)
    return render_markdown(report)


def report_path(out, target_input):
    if out.lower().endswith(".md"):
        return out
    filename = f"IMPACT_REPORT_{sanitize_target(target_input)}.md"
    return os.path.join(out, filename)


def write_report(report, out):
    path = report_path(out, report["target"]["input"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_report_markdown(report))
    return path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    parser.add_argument("--out", default=os.path.join(config.INDEX_DIR, "reports"))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        report = build_report(args.target)
    except (FileNotFoundError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1

    print(render_report_markdown(report), end="")
    if args.out:
        path = write_report(report, args.out)
        print("")
        print(f"Wrote {display_path(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
