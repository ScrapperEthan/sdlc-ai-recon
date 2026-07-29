#!/usr/bin/env python3
"""Derive delivery-job/vendor topology from repository names (read-only inputs)."""
import argparse
import csv
import json
import os
import re
from collections import defaultdict

from retriever import config
from retriever.vendors import (  # noqa: F401 — re-exported: callers/tests use these names here
    KNOWN_VENDORS,
    UNKNOWN_VENDOR,
    VENDOR_ALIASES,
    canon_vendor,
    pick_vendor as _pick_vendor,
)

CHANNELS = ("sms", "mms", "email", "letter", "whatsapp", "wechat", "push")
_CHANNEL_RE = "|".join(CHANNELS)
# A message-type qualifier can sit between the vendor and the channel (e.g. `htcl-2way-sms`).
# Capture it as an optional group so the vendor token is the carrier (`htcl`), not the qualifier
# (`2way`). 2-way SMS is a 3HK/htcl flow (owner-confirmed 2026-07-23) — with canon_vendor(htcl)=3hk
# this is what folds those jobs under the 3HK vendor instead of a phantom `2way` bucket.
MSG_QUALIFIERS = "2way"
DELIVERY_RE = re.compile(
    rf"^(?P<prefix>.+?)-(?P<vendor>[a-z0-9]+)(?:-(?P<qualifier>{MSG_QUALIFIERS}))?-(?P<channel>{_CHANNEL_RE})-deli-job$",
    re.I,
)
OUTBOUND_RE = re.compile(r"-(?P<vendor>[a-z0-9-]+)-outbound-api$", re.I)

# The vendor vocabulary (KNOWN_VENDORS / VENDOR_ALIASES / canon_vendor / _pick_vendor) moved to
# `retriever/vendors.py` and is re-exported above: the consumption side needs the same fold at
# query time (retriever/delivery_chain.py), and two copies of a carrier whitelist is exactly how a
# vendor silently splits into two buckets again. See that module for the RUNBOOK-49/51/52 history.


def load_json(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def repo_universe(edges_path, repo_tags_path=None):
    repos = set()
    try:
        with open(edges_path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                for column in ("from_repo", "to_repo"):
                    repo = (row.get(column) or "").strip()
                    if repo:
                        repos.add(repo)
    except FileNotFoundError:
        pass
    tags = load_json(repo_tags_path) if repo_tags_path else {}
    if isinstance(tags, dict):
        repos.update(str(repo).strip() for repo in tags if str(repo).strip())
    return sorted(repos)


def _deep_merge(base, overlay):
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    merged = dict(base)
    for key, value in overlay.items():
        merged[key] = _deep_merge(merged[key], value) if key in merged else value
    return merged


def _by_repo(topology):
    result = {}
    for channel, vendors in topology.items():
        if channel == "by_repo" or not isinstance(vendors, dict):
            continue
        for vendor, group in vendors.items():
            if not isinstance(group, dict):
                continue
            for job in group.get("delivery_jobs") or []:
                if isinstance(job, dict) and job.get("repo"):
                    result[job["repo"]] = dict(job, kind="delivery-job", channel=channel, vendor=vendor)
            for api in group.get("outbound_apis") or []:
                if isinstance(api, dict) and api.get("repo"):
                    result[api["repo"]] = dict(api, kind="outbound-api", channel=channel, vendor=vendor)
    return result


def build_topology(
    edges_path=config.EDGES_CSV,
    override_path=config.DELIVERY_TOPOLOGY_OVERRIDE_JSON,
    repo_tags_path=config.REPO_TAGS_JSON,
):
    topology = defaultdict(lambda: defaultdict(lambda: {"delivery_jobs": [], "outbound_apis": []}))
    unmatched_delivery, unmatched_outbound = [], []
    outbound_repos = []
    for repo in repo_universe(edges_path, repo_tags_path):
        delivery = DELIVERY_RE.match(repo)
        if delivery:
            channel = delivery.group("channel").lower()
            # Every token before the channel (the regex's `vendor` group is just the last one) is a
            # vendor candidate; the real carrier is the rightmost KNOWN one. Picking positionally
            # promoted mode/system words (hr, hase) and split ICCM variants into phantom vendors.
            pre_channel = [t for t in f"{delivery.group('prefix')}-{delivery.group('vendor')}".split("-") if t]
            vendor = _pick_vendor(pre_channel)
            job = {"repo": repo, "channel": channel, "vendor": vendor, "name_tokens": [t.lower() for t in pre_channel]}
            qualifier = (delivery.group("qualifier") or "").lower()
            if qualifier:
                job["message_type"] = qualifier
            topology[channel][vendor]["delivery_jobs"].append(job)
            continue
        outbound = OUTBOUND_RE.search(repo)
        if outbound:
            vendor = canon_vendor(outbound.group("vendor").lower())
            outbound_repos.append({"repo": repo, "vendor": vendor})
            continue
        if repo.endswith("-deli-job"):
            unmatched_delivery.append(repo)
        if repo.endswith("-outbound-api"):
            unmatched_outbound.append(repo)

    known_vendors = {vendor for vendors in topology.values() for vendor in vendors}
    for api in outbound_repos:
        # Outbound names often carry a system prefix (for example mc-x-sinch-outbound-api).
        # Prefer a vendor already discovered structurally from a delivery job, rather than
        # mistaking that entire prefixed stem for the vendor. Canonicalize tokens too, so an
        # alias in the name (mc-hk-hase-htcl-outbound-api) matches its canonical vendor bucket.
        repo_tokens = {canon_vendor(token) for token in api["repo"].lower().split("-")}
        candidates = sorted((vendor for vendor in known_vendors if vendor in repo_tokens), key=len, reverse=True)
        if candidates:
            api["vendor"] = candidates[0]
        matching_channels = [
            channel for channel, vendors in topology.items()
            if api["vendor"] in vendors and channel != "unassigned"
        ]
        for channel in matching_channels or ["unassigned"]:
            topology[channel][api["vendor"]]["outbound_apis"].append(dict(api))

    payload = {
        channel: {vendor: dict(group) for vendor, group in sorted(vendors.items())}
        for channel, vendors in sorted(topology.items())
    }
    # Allow a small hand-curated overlay for non-name-revealing links or topic patterns.
    payload = _deep_merge(payload, load_json(override_path))
    payload["by_repo"] = _by_repo(payload)
    return payload, unmatched_delivery, unmatched_outbound


def coverage(payload, unmatched_delivery, unmatched_outbound):
    channels = [key for key in payload if key not in {"by_repo", "unassigned"}]
    vendors = set()
    jobs = apis = 0
    for channel, groups in payload.items():
        if channel == "by_repo" or not isinstance(groups, dict):
            continue
        for vendor, group in groups.items():
            if not isinstance(group, dict):
                continue
            vendors.add(vendor)
            jobs += len(group.get("delivery_jobs") or [])
            apis += len(group.get("outbound_apis") or [])
    return [("channels", len(channels)), ("vendors", len(vendors)), ("delivery_jobs", jobs),
            ("outbound_apis", apis), ("unparsed_deli_jobs", len(unmatched_delivery)),
            ("unparsed_outbound_apis", len(unmatched_outbound))]


def write_payload(payload, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", default=config.EDGES_CSV)
    parser.add_argument("--override", default=config.DELIVERY_TOPOLOGY_OVERRIDE_JSON)
    parser.add_argument("--repo-tags", default=config.REPO_TAGS_JSON)
    parser.add_argument("--out", default=config.DELIVERY_TOPOLOGY_JSON)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    payload, unmatched_delivery, unmatched_outbound = build_topology(args.edges, args.override, args.repo_tags)
    write_payload(payload, args.out)
    print("Delivery topology coverage")
    for label, count in coverage(payload, unmatched_delivery, unmatched_outbound):
        print(f"{label:24} {count}")
    if unmatched_delivery:
        print("Unparsed *-deli-job: " + ", ".join(unmatched_delivery))
    if unmatched_outbound:
        print("Unparsed *-outbound-api: " + ", ".join(unmatched_outbound))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
