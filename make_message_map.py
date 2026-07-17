#!/usr/bin/env python3
"""Extract async message wiring (repo -> topic/queue -> channel) from source.

Built against RUNBOOK-26 discovery: this estate is config-driven, NOT Spring-Kafka annotations.
Destinations live mostly in ``application.yml`` (``consumerInformationList: topicName:``,
``listener.<x>.queue:``) with a few Java constants, and follow the HRN dotted convention where the
CHANNEL is encoded in the name (``hrn.hsbc.wpb.notification.…-csl_svc_rt_sms`` -> sms). So we scan
yml/properties/java for destination literals, guess produce/consume from nearby markers, derive the
channel from the name (or the vendor token), and emit:

- ``index/message_edges.csv``  (producer_repo, destination, consumer_repo, routing_source, evidence)
- ``index/message_channels.json``  {repo: {channels, destinations:[{name, kind, role, channel, evidence}]}}

stdlib only, read-only over the mirror.
"""
import argparse
import csv
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

from retriever import config

_SKIP = {".git", "target", "build", "node_modules", ".codegraph"}
_TEXT_EXT = (".yml", ".yaml", ".properties", ".java")
_TEST_RE = re.compile(r"(^|/)(test|tests|it)(/|$)", re.I)

# Destination literals (RUNBOOK-26 samples).
HRN_RE = re.compile(r"hrn\.[a-z0-9][\w.\-]+", re.I)                 # topic: hrn.hsbc.wpb.notification…
QUEUE_APP_RE = re.compile(r"\bq_[a-z0-9][\w.\-]*", re.I)            # queue: q_csl_tracking
QUEUE_MQ_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:\.[A-Z0-9_]+){2,}\b")  # legacy MQ: TLXNCAR.SASP…HASE_SMS_REQ

# Channel is encoded in the destination name; fall back to a vendor token -> channel.
CHANNEL_TOKENS = ("whatsapp", "wechat", "email", "letter", "push", "mms", "sms")
VENDOR_CHANNEL = {
    "csl": "sms", "sinch": "sms", "3hk": "sms", "pfp": "email", "proofpoint": "email",
    "haro": "whatsapp", "iccm": "letter", "otx": "letter", "sns": "push", "apns": "push", "fcm": "push",
}
CONSUME_MARKERS = ("consumerinformationlist", "@jmslistener", "@kafkalistener", "consumergroup", "listener")
PRODUCE_MARKERS = ("convertandsend", ".send(", "producer", "publishmessage", "gettopicname")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def channel_of(name):
    low = name.lower()
    for channel in CHANNEL_TOKENS:
        if channel in low:
            return channel
    for vendor, channel in VENDOR_CHANNEL.items():
        if vendor in low:
            return channel
    return ""


def _kind(name):
    if HRN_RE.fullmatch(name):
        return "topic"
    return "queue"


def _iter_files(repo_root):
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for name in filenames:
            if name.endswith(_TEXT_EXT):
                path = os.path.join(dirpath, name)
                if not _TEST_RE.search(path.replace(os.sep, "/")):
                    yield path


def _role(lower_line):
    consume = any(marker in lower_line for marker in CONSUME_MARKERS)
    produce = any(marker in lower_line for marker in PRODUCE_MARKERS)
    if consume and not produce:
        return "consume"
    if produce and not consume:
        return "produce"
    return "reference"


def scan_repo(repo, repo_root):
    """Return {destination: {kind, role, channel, evidence}} for one repo (first evidence wins)."""
    found = {}
    for path in _iter_files(repo_root):
        rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        # A small window of context around each hit disambiguates produce vs consume.
        for index, line in enumerate(lines):
            window = " ".join(lines[max(0, index - 2): index + 3]).lower()
            for regex in (HRN_RE, QUEUE_APP_RE, QUEUE_MQ_RE):
                for match in regex.findall(line):
                    name = match.strip().strip('".,;')
                    if len(name) < 6 or name in found:
                        continue
                    found[name] = {
                        "kind": _kind(name),
                        "role": _role(window),
                        "channel": channel_of(name),
                        "evidence": f"{repo}/{rel}:{index + 1}",
                    }
    return found


def build(mirror):
    repos = {}
    if os.path.isdir(mirror):
        for name in sorted(os.listdir(mirror)):
            repo_root = os.path.join(mirror, name)
            if os.path.isdir(repo_root) and not name.startswith("."):
                destinations = scan_repo(name, repo_root)
                if destinations:
                    repos[name] = destinations
    return repos


def to_channels(repos):
    payload = {}
    for repo, destinations in repos.items():
        channels = sorted({meta["channel"] for meta in destinations.values() if meta["channel"]})
        payload[repo] = {
            "channels": channels,
            "destinations": [
                {"name": name, **meta} for name, meta in sorted(destinations.items())
            ],
        }
    return payload


def to_edges(repos):
    """Cross-repo edges: pair each destination's producers with its consumers (best-effort)."""
    by_dest = defaultdict(lambda: {"produce": [], "consume": [], "reference": [], "evidence": ""})
    for repo, destinations in repos.items():
        for name, meta in destinations.items():
            group = by_dest[name]
            group[meta["role"]].append(repo)
            group["evidence"] = group["evidence"] or meta["evidence"]
    rows = []
    for destination, group in sorted(by_dest.items()):
        producers = group["produce"] or [""]
        consumers = group["consume"] + group["reference"] or [""]
        for producer in producers:
            for consumer in consumers:
                if producer or consumer:
                    rows.append({
                        "producer_repo": producer, "destination": destination,
                        "consumer_repo": consumer, "routing_source": "source-scan",
                        "evidence": group["evidence"],
                    })
    return rows


def coverage_rows(channels_payload, repo_tags):
    repos_with_dest = len(channels_payload)
    repos_with_channel = sum(1 for meta in channels_payload.values() if meta["channels"])
    newly = 0
    if isinstance(repo_tags, dict):
        for repo, meta in channels_payload.items():
            tag = repo_tags.get(repo) or {}
            if meta["channels"] and not (tag.get("channel") or []):
                newly += 1
    return [
        ("repos_with_destinations", repos_with_dest),
        ("repos_with_channel_via_msg", repos_with_channel),
        ("channel_unknown_now_covered", newly),
    ]


# The first five columns are the stable contract (retriever/messages.py reads only these); the rest
# are additive producer evidence from producer_extract, blank for consumer-scan rows.
EDGE_FIELDS = [
    "producer_repo", "destination", "consumer_repo", "routing_source", "evidence",
    "producer_type", "producer_symbol", "call_site", "destination_expression",
    "destination_kind", "confidence", "resolution_status",
]


def write_csv(rows, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EDGE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _consumer_map(repos):
    """destination -> repos that consume it, from the existing config scan (for producer pairing)."""
    out = defaultdict(list)
    for repo, destinations in repos.items():
        for name, meta in destinations.items():
            if meta["role"] in ("consume", "reference"):
                out[name].append(repo)
    return out


def producer_edges(records, consumer_map):
    """Turn producer_extract records into CSV rows: a resolved destination pairs with each known
    consumer; an unresolved one is still emitted (consumer blank) so producer coverage isn't lost."""
    rows, seen = [], set()
    for record in records:
        destination = record.get("destination") or ""
        consumers = consumer_map.get(destination) if destination else None
        for consumer in (consumers or [""]):
            row = dict(record)
            row["consumer_repo"] = consumer
            key = (row.get("producer_repo"), destination, consumer, row.get("call_site"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def write_json(payload, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", default=config.MIRROR)
    parser.add_argument("--edges-out", default=config.MESSAGE_EDGES_CSV)
    parser.add_argument("--channels-out", default=os.path.join(config.INDEX_DIR, "message_channels.json"))
    parser.add_argument("--repo-tags", default=config.REPO_TAGS_JSON)
    return parser.parse_args(argv)


def main(argv=None):
    import producer_extract  # lazy: avoids a top-level import cycle (producer_extract imports us)
    args = parse_args(argv)
    repos = build(args.mirror)
    channels = to_channels(repos)
    edges = to_edges(repos)
    # Signature/wrapper-aware producer edges (see docs/specs/producer-coverage.md). Additive: the
    # config scan above is unchanged; these rows fill the sparse producer side.
    producer_rows = producer_edges(producer_extract.scan_producers(args.mirror), _consumer_map(repos))
    write_json({"generated_at": _now(), "repos": channels}, args.channels_out)
    write_csv(edges + producer_rows, args.edges_out)
    rows = coverage_rows(channels, load_json(args.repo_tags))
    print("Message-map coverage\n")
    for label, count in rows:
        print(f"{label:32} {count:6d}")
    producer_repos = {row["producer_repo"] for row in producer_rows if row.get("producer_repo")}
    resolved = sum(1 for row in producer_rows if row.get("resolution_status") == "resolved")
    print(f"{'producer_edges_extracted':32} {len(producer_rows):6d}")
    print(f"{'producer_repos':32} {len(producer_repos):6d}")
    print(f"{'producer_edges_resolved_dest':32} {resolved:6d}")
    print(f"\nWrote {args.edges_out}\nWrote {args.channels_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
