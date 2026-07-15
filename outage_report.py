#!/usr/bin/env python3
"""Build cited delivery channel/vendor outage impact reports from local artifacts."""
import argparse
import json
import os
import re
from collections import defaultdict

from retriever import config, graph, messages, repo_tags


CHANNELS = ("sms", "mms", "email", "letter", "whatsapp", "wechat", "push")
SOURCE_PRIORITY = {"message-map": 3, "channel-token": 2, "token-heuristic": 1}
GENERIC_NAME_TOKENS = {"mc", "hk", "x", "amet", "mdc", "hsbc"}
# "other"/"others" is a sheet bucket, never a delivery channel — mirror make_repo_tags /
# make_arch_map so serves_channels can never surface it. The data is already clean; keep the
# guard defensive.
NON_CHANNELS = frozenset({"other", "others", "unknown", "n/a", ""})
# Preferred order for grouping the affected-repo list and the by_relation breakdown:
# owners first, then library blast-radius, then the delivery chain, then the dependency closure.
RELATION_ORDER = (
    "channel-owner",
    "serves-channel",
    "msg-channel",
    "delivery-job",
    "outbound-api",
    "dependency-upstream",
    "dependency-downstream",
)


def display_path(path):
    try:
        path = os.path.relpath(path, config.ROOT)
    except ValueError:
        pass
    return path.replace(os.sep, "/")


def parse_target(raw):
    text = (raw or "").strip()
    if ":" not in text:
        raise ValueError("target must be channel:<channel>, vendor:<vendor>, or repo:<delivery-job>")
    kind, value = (part.strip() for part in text.split(":", 1))
    kind, value = kind.lower(), value.lower()
    if kind not in {"channel", "vendor", "repo"} or not value:
        raise ValueError("target must be channel:<channel>, vendor:<vendor>, or repo:<delivery-job>")
    return kind, value


def load_topology():
    try:
        with open(config.DELIVERY_TOPOLOGY_JSON, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError as error:
        raise FileNotFoundError("no delivery_topology.json; run make_delivery_topology.py first") from error
    except json.JSONDecodeError as error:
        raise ValueError("invalid delivery_topology.json") from error
    if not isinstance(data, dict):
        raise ValueError("invalid delivery_topology.json")
    return data


def topology_groups(topology, kind, value):
    selected = []
    for channel, vendors in topology.items():
        if channel == "by_repo" or not isinstance(vendors, dict):
            continue
        for vendor, group in vendors.items():
            if not isinstance(group, dict):
                continue
            jobs = [job for job in group.get("delivery_jobs") or [] if isinstance(job, dict)]
            if kind == "channel" and channel.lower() == value:
                selected.append((channel, vendor, group, jobs))
            elif kind == "vendor" and vendor.lower() == value:
                selected.append((channel, vendor, group, jobs))
            elif kind == "repo" and any((job.get("repo") or "").lower() == value for job in jobs):
                selected.append((channel, vendor, group, jobs))
    if kind == "channel" and value not in CHANNELS:
        raise ValueError(f"unknown channel: {value}")
    if not selected:
        raise FileNotFoundError(f"unknown {kind}: {value}")
    return selected


def _topic_tokens(topic):
    return {part.lower() for part in re.split(r"[^a-zA-Z0-9]+", topic or "") if part}


def _message_map_topics(jobs):
    job_names = {(job.get("repo") or "").lower() for job in jobs}
    results = []
    try:
        import csv
        with open(config.MESSAGE_EDGES_CSV, newline="", encoding="utf-8-sig") as handle:
            for line_no, row in enumerate(csv.DictReader(handle), 2):
                if (row.get("consumer_repo") or "").strip().lower() not in job_names:
                    continue
                topic = (row.get("destination") or "").strip()
                if topic:
                    results.append({
                        "topic": topic,
                        "source": "message-map",
                        "confidence": "high",
                        "matched_jobs": [(row.get("consumer_repo") or "").strip()],
                        "citations": [f"{display_path(config.MESSAGE_EDGES_CSV)}:{line_no}"],
                    })
    except FileNotFoundError:
        pass
    return results


def _topic_rows_for_channel(channel):
    rows = messages.use_cases_for_channel(channel)
    grouped = defaultdict(lambda: {"citations": set(), "use_cases": set()})
    for row in rows:
        item = grouped[row["topic"]]
        item["citations"].update(row["citations"])
        item["use_cases"].add(row["use_case"])
    return grouped


def _pattern_matches(topic, patterns):
    text = (topic or "").lower()
    for pattern in patterns or []:
        pattern = str(pattern).strip().lower()
        if not pattern:
            continue
        try:
            if re.search(pattern, text):
                return True
        except re.error:
            if pattern in text:
                return True
    return False


def resolve_topics_for(target):
    """Resolve topology target to snapshot topics, preferring real message-map edges."""
    kind, value = parse_target(target)
    topology = load_topology()
    groups = topology_groups(topology, kind, value)
    jobs = [job for _channel, _vendor, _group, items in groups for job in items]
    channels = sorted({channel for channel, _vendor, _group, _items in groups if channel != "unassigned"})
    resolved = {}

    def add(item):
        key = item["topic"].lower()
        prior = resolved.get(key)
        if not prior or SOURCE_PRIORITY[item["source"]] > SOURCE_PRIORITY[prior["source"]]:
            resolved[key] = item
        elif SOURCE_PRIORITY[item["source"]] == SOURCE_PRIORITY[prior["source"]]:
            prior["citations"] = sorted(set(prior["citations"]) | set(item["citations"]))
            prior["matched_jobs"] = sorted(set(prior["matched_jobs"]) | set(item["matched_jobs"]))

    for item in _message_map_topics(jobs):
        add(item)

    if kind == "channel":
        for topic, meta in _topic_rows_for_channel(value).items():
            add({
                "topic": topic,
                "source": "channel-token",
                "confidence": "high",
                "matched_jobs": sorted((job.get("repo") or "") for job in jobs),
                "citations": sorted(meta["citations"]),
            })
    else:
        for channel in channels:
            for topic, meta in _topic_rows_for_channel(channel).items():
                topic_tokens = _topic_tokens(topic)
                matching_jobs = []
                for job in jobs:
                    shared = sorted((set(job.get("name_tokens") or []) & topic_tokens) - GENERIC_NAME_TOKENS)
                    if len(shared) >= 2:
                        matching_jobs.append(job.get("repo") or "")
                if matching_jobs:
                    add({
                        "topic": topic,
                        "source": "token-heuristic",
                        "confidence": "heuristic",
                        "matched_jobs": sorted(matching_jobs),
                        "citations": sorted(meta["citations"]),
                    })
                elif any(_pattern_matches(topic, group.get("topic_patterns")) for _c, _v, group, _j in groups):
                    # Curated patterns are still an interim bridge, not message-map evidence.
                    add({
                        "topic": topic,
                        "source": "token-heuristic",
                        "confidence": "heuristic",
                        "matched_jobs": [],
                        "citations": sorted(meta["citations"]) + [display_path(config.DELIVERY_TOPOLOGY_JSON)],
                    })
    return {
        "target": {"kind": kind, "value": value},
        "channels": channels,
        "groups": groups,
        "topics": sorted(resolved.values(), key=lambda item: item["topic"].lower()),
    }


def affected_use_cases(topics):
    grouped = {}
    for topic in topics:
        for row in messages.use_cases_for_topic(topic["topic"]):
            entry = grouped.setdefault(row["use_case"], {"use_case": row["use_case"], "topics": [], "citations": set()})
            entry["topics"].append(topic["topic"])
            entry["citations"].update(row["citations"])
    return [
        {"use_case": item["use_case"], "topics": sorted(set(item["topics"])), "citations": sorted(item["citations"])}
        for item in sorted(grouped.values(), key=lambda item: item["use_case"].lower())
    ]


def _clean_channels(values):
    """Lowercased channel set with the sheet buckets (other/others/…) stripped out."""
    return {
        str(value).strip().lower()
        for value in (values or [])
        if str(value).strip().lower() not in NON_CHANNELS
    }


def serves_channel_repos(channels):
    """Repos that own or serve the outage's channels, from the pre-computed repo_tags index.

    For the resolved channel set, return ``{repo: row}`` where a row cites ``index/repo_tags.json``
    (path only — it's a generated artifact) and carries the strongest relation:
    - ``channel-owner``  — the repo's own ``channel`` tags intersect the outage channels.
    - ``serves-channel`` — else its ``serves_channels`` (Maven library blast-radius) intersect them.
    - ``msg-channel``    — else its ``msg_channels`` (async topic/queue wiring) intersect them, i.e.
      a messaging-only repo the Maven graph can't reach (from make_message_map).
    Empty/missing index → ``{}`` (never crash). ``other``/``others`` never count as a channel.
    """
    wanted = _clean_channels(channels)
    if not wanted:
        return {}
    citation = display_path(config.REPO_TAGS_JSON)
    rows = {}
    for repo, meta in repo_tags.load().items():
        if _clean_channels(meta.get("channel")) & wanted:
            relation = "channel-owner"
        elif _clean_channels(meta.get("serves_channels")) & wanted:
            relation = "serves-channel"
        elif _clean_channels(meta.get("msg_channels")) & wanted:
            relation = "msg-channel"
        else:
            continue
        rows[repo] = {"repo": repo, "relation": relation, "citations": [citation]}
    return rows


def affected_repos(groups, channels=None):
    rows = {}
    topo_citation = display_path(config.DELIVERY_TOPOLOGY_JSON)
    seed_jobs = []
    for channel, vendor, group, jobs in groups:
        for job in jobs:
            repo = job.get("repo") or ""
            if repo:
                seed_jobs.append(repo)
                rows[repo] = {"repo": repo, "relation": "delivery-job", "citations": [topo_citation]}
        for api in group.get("outbound_apis") or []:
            repo = api.get("repo") if isinstance(api, dict) else ""
            if repo:
                rows.setdefault(repo, {"repo": repo, "relation": "outbound-api", "citations": [topo_citation]})
    for seed in seed_jobs:
        closure = graph.impact(seed, transitive=True)
        for relation, names in (("dependency-upstream", closure["depended_on_by"]), ("dependency-downstream", closure["depends_on"])):
            for repo in names:
                rows.setdefault(repo, {"repo": repo, "relation": relation, "citations": [display_path(config.EDGES_CSV)]})
    # Fold in the channel blast-radius (owners + serving libs) under the topology-derived rows:
    # setdefault means a repo already labelled delivery-job/outbound-api/dependency-* keeps that label.
    for repo, row in serves_channel_repos(channels).items():
        rows.setdefault(repo, row)
    return [rows[name] for name in sorted(rows)]


def _relation_rank(relation):
    try:
        return RELATION_ORDER.index(relation)
    except ValueError:
        return len(RELATION_ORDER)


def count_by_relation(repos):
    """Ordered {relation: count} over the affected-repo rows; counts sum to len(repos)."""
    counts = {}
    for item in repos:
        relation = item.get("relation", "")
        counts[relation] = counts.get(relation, 0) + 1
    ordered = {relation: counts[relation] for relation in RELATION_ORDER if relation in counts}
    for relation in sorted(counts):
        ordered.setdefault(relation, counts[relation])
    return ordered


def build_report(target):
    resolved = resolve_topics_for(target)
    topics = resolved["topics"]
    use_cases = affected_use_cases(topics)
    kind = resolved["target"]["kind"]
    # The channel blast-radius (channel-owner + serves-channel libs + msg-only repos) is a
    # CHANNEL-level concept. Folding it into a single-VENDOR outage over-counts wildly: Sinch is
    # one of several SMS vendors, yet it would pull in EVERY sms repo — including CSL/CM delivery
    # jobs a Sinch outage can't touch. Scope vendor/repo outages to that target's own delivery +
    # outbound repos and their dependency closure; only a channel outage folds the blast-radius in.
    channels_for_repos = resolved["channels"] if kind == "channel" else None
    repos = affected_repos(resolved["groups"], channels_for_repos)
    confidence = (
        "渠道级影响基于路由快照中的 channel token，当前数据下为可靠结论。"
        if kind == "channel" else
        "供应商/分支级主题归属目前按名称 token 推断；在完整 message map 提供 topic→delivery-job 边前应视为启发式结论。"
    )
    citations = sorted({citation for topic in topics for citation in topic["citations"]} |
                       {citation for item in use_cases for citation in item["citations"]} |
                       {citation for item in repos for citation in item["citations"]})
    return {
        "target": {"input": target, **resolved["target"], "channels": resolved["channels"]},
        "confidence": "high" if kind == "channel" else "heuristic",
        "confidence_banner": confidence,
        "confidence_citations": (
            sorted({citation for topic in topics for citation in topic["citations"]})
            if kind == "channel" else [display_path(config.DELIVERY_TOPOLOGY_JSON)]
        ),
        "affected_topics": topics,
        "affected_use_cases": {"count": len(use_cases), "items": use_cases},
        "affected_repos": {"count": len(repos), "items": repos, "by_relation": count_by_relation(repos)},
        "citations": citations,
    }


def render_markdown(report):
    target = report["target"]
    lines = [
        f"# Outage Impact — {target['input']}", "",
        "## Confidence", report["confidence_banner"],
        "Citations: " + ", ".join(report.get("confidence_citations") or ["none"]), "",
        "## Affected topics",
    ]
    lines.extend([
        f"- `{item['topic']}` — {item['source']} ({item['confidence']}); citations: {', '.join(item['citations'])}"
        for item in report["affected_topics"]
    ] or ["- none"])
    lines.extend(["", f"## Affected use-cases ({report['affected_use_cases']['count']})"])
    lines.extend([
        f"- `{item['use_case']}` via {', '.join(item['topics'])}; citations: {', '.join(item['citations'])}"
        for item in report["affected_use_cases"]["items"]
    ] or ["- none"])
    lines.extend(["", f"## Affected repos/components ({report['affected_repos']['count']})"])
    lines.append(
        "图例 / Legend: `channel-owner` 拥有该渠道 · `serves-channel` 故障波及(库级) · "
        "`delivery-job`/`outbound-api` 投递链 · `dependency-*` 依赖闭包"
    )
    repo_items = sorted(
        report["affected_repos"]["items"],
        key=lambda item: (_relation_rank(item.get("relation", "")), item.get("relation", ""), item.get("repo", "")),
    )
    lines.extend([
        f"- `{item['repo']}` — {item['relation']}; citations: {', '.join(item['citations'])}"
        for item in repo_items
    ] or ["- none"])
    lines.extend(["", "## Citations"])
    lines.extend(f"- {citation}" for citation in report["citations"])
    return "\n".join(lines).rstrip() + "\n"


def _safe_name(target):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", target).strip("_") or "target"


def write_report(report, out):
    os.makedirs(out, exist_ok=True)
    base = "OUTAGE_IMPACT_" + _safe_name(report["target"]["input"])
    markdown_path = os.path.join(out, base + ".md")
    json_path = os.path.join(out, base + ".json")
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return markdown_path, json_path


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
    print(render_markdown(report), end="")
    markdown_path, json_path = write_report(report, args.out)
    print(f"Wrote {display_path(markdown_path)}")
    print(f"Wrote {display_path(json_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
