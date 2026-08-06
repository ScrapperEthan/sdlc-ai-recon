"""Turning "376 downstream" into an answer someone can act on.

## The problem this exists for

Ask the impact view about `mc-hk-hase-api-common` and it says **376 downstream repos**. That number
is correct and almost useless. 376 out of ~460 is not a blast radius — it is a restatement of the
fact that the repo is shared infrastructure. Worse, it is *unactionable in exactly the case people
ask about most*: shared libraries and parents are the repos a change-notification question is
usually about.

A useful answer to "what breaks if I change this" has a different shape depending on which repo you
asked about, and the engine has to notice which one it is:

* **A leaf service** (12 downstream) — list them. The list IS the answer.
* **Shared infrastructure** (376 downstream) — the list is noise. What a human needs is: who
  depends on it DIRECTLY (that is the notification list), which delivery channels are represented
  (that is what could stop working, in business terms), and which of the affected repos are
  independently critical.

## What this module does NOT do

It does not drop anything. Every downstream repo stays in the report's `downstream` array exactly
as before — this is an additional summary, so a caller that wants the full list still has it. The
change is what the *narrative* leads with.

It also does not invent a channel. A repo whose channel is unknown is counted as unknown and said
out loud, because quietly reporting "3 channels affected" out of a set where most repos have no
channel tag would be a confident undercount. That is the same failure this codebase keeps closing:
silence reading as absence.

## The evidence layer (RUNBOOK-77)

The channel spread used to be name-plus-sheet only, so it was a string match on a repo name wearing
the authority of a business statement. `retriever/channel_evidence.py` adds scanned source/config
evidence with a citation, which changes two things here:

* a channel can now be reported with **why** — `direct_code_evidence` reads differently from
  `name_derived`, and collapsing them back into one list is what made the old answer feel general;
* the unknown count splits into states with different remedies — *scanned and clean* is a finding,
  *outside the scan's scope* is not a gap, and *no scope file* means we cannot tell. A single
  "unknown" number bundles a fact, a non-issue and an ignorance together.

`transitive_dependency` and `message_carried` stay OUT of the spread count. The first is derived
from the very graph relationship being measured, so counting it lets a repo inherit a channel from
the edge under test and hand it back as independent evidence. The second is coupling, not ownership.
"""
from . import channel_evidence, criticality, graph, repo_tags

# A repo reaching this share of the estate is telling you about ITSELF (it is shared), not about the
# change you asked about. Not a magic constant so much as the point where enumerating stops helping:
# a fifth of the estate cannot be a notification list, a review list, or a test plan.
HUB_SHARE = 0.20
# Below this the full list is genuinely the answer, so no summary shape-change is warranted even if
# the share calculation is unavailable (tiny or missing estate).
SMALL_ENOUGH_TO_LIST = 25

_UNKNOWN = "unknown"


def _owned_channels(repo, tags, evidence):
    """The repo's OWN channels, each with the strongest relation that says so.

    Ownership only: `message_carried` (coupling) and `transitive_dependency` (the graph edge being
    measured) are filtered out — see the module docstring for why the second would be circular.
    Returns `{channel: {"relation": ..., "confidence": ...}}`.
    """
    view = channel_evidence.for_repo(repo, tags=tags, evidence=evidence)
    return {
        row["channel"]: {"relation": row["relation"], "confidence": row["confidence"]}
        for row in view if row["direct"]
    }


def summarise(repo, downstream, tags=None, top_critical=10, evidence=None, scope=None):
    """Blast radius for one repo -> a shape verdict plus the three things worth acting on.

    `downstream` is the report's existing list of `{repo, relation, ...}` items; nothing is removed
    from it. Returns a dict that is safe to render even when repo_tags, the evidence file or the
    criticality inputs are absent (each degrades to "unknown", never to a smaller-looking number).
    """
    tags = repo_tags.load() if tags is None else tags
    evidence = channel_evidence.load() if evidence is None else evidence
    scope = channel_evidence.load_scope() if scope is None else scope
    items = [item for item in downstream or [] if isinstance(item, dict) and item.get("repo")]
    direct = [item["repo"] for item in items if item.get("relation") == "direct"]
    transitive = [item["repo"] for item in items if item.get("relation") != "direct"]
    total = len(items)

    estate = len(graph.known_repos() or ()) or len(tags) or 0
    share = (total / estate) if estate else None
    is_hub = bool(share is not None and share >= HUB_SHARE and total > SMALL_ENOUGH_TO_LIST)

    by_channel = {}
    unknown_channel = 0
    # The unknown count is only honest if it says WHICH kind of unknown. "Scanned and clean" is a
    # finding, "outside the scan scope" is not a defect, and without a scope file the two cannot be
    # told apart at all — three different sentences, not one number.
    unknown_kinds = {"scanned_clean": 0, "out_of_scope": 0, "scope_unknown": 0}
    for item in items:
        channels = _owned_channels(item["repo"], tags, evidence)
        if not channels:
            unknown_channel += 1
            if not scope.get("known"):
                unknown_kinds["scope_unknown"] += 1
            elif item["repo"] in scope["scanned"]:
                unknown_kinds["scanned_clean"] += 1
            else:
                unknown_kinds["out_of_scope"] += 1
            continue
        for channel, how in channels.items():
            bucket = by_channel.setdefault(channel, {"repos": 0, "relations": {}})
            bucket["repos"] += 1
            bucket["relations"][how["relation"]] = bucket["relations"].get(how["relation"], 0) + 1

    channels_out = []
    for name, bucket in by_channel.items():
        strongest = min(bucket["relations"], key=lambda rel: _RELATION_STRENGTH.get(rel, 99))
        channels_out.append({
            "channel": name,
            "repos": bucket["repos"],
            "strongest_relation": strongest,
            "by_relation": dict(sorted(bucket["relations"].items())),
            "code_backed": bucket["relations"].get("direct_code_evidence", 0)
            + bucket["relations"].get("direct_config_evidence", 0),
        })
    channels_out.sort(key=lambda row: (-row["repos"], row["channel"]))

    return {
        "repo": repo,
        "total": total,
        "direct": sorted(direct),
        "direct_count": len(direct),
        "transitive_count": len(transitive),
        "estate": estate,
        "share_of_estate": round(share, 4) if share is not None else None,
        "is_hub": is_hub,
        "channels": channels_out,
        "channel_unknown_repos": unknown_channel,
        "channel_unknown_breakdown": unknown_kinds,
        "evidence_available": bool(evidence.get("readable")),
        "scope_known": bool(scope.get("known")),
        "notable": _notable(items, top_critical),
        "reading": _reading(repo, total, len(direct), is_hub, share, unknown_channel,
                            unknown_kinds, bool(scope.get("known"))),
    }


# Ordering used only to pick the label for a channel that several repos reach in different ways.
# The authoritative hierarchy lives in config/channel_evidence.json; this mirrors its default order
# and is a display concern, so a box that reorders the contract does not silently change counts.
_RELATION_STRENGTH = {
    "direct_code_evidence": 0,
    "direct_config_evidence": 1,
    "business_declared": 2,
    "name_derived": 3,
}

# Prose, because "name_derived" on a page reads as a field name and a reader has to be told that the
# strongest thing behind a channel is a substring of a repo name.
_RELATION_LABEL = {
    "direct_code_evidence": "cited source evidence",
    "direct_config_evidence": "cited config evidence",
    "business_declared": "declared by the business sheet",
    "name_derived": "inferred from the repo name only",
}


def _notable(items, top_critical):
    """Downstream repos that are independently critical, with the axis that says so.

    Uses the existing multi-axis ranking rather than re-deriving one. Criticality being unavailable
    yields an empty list and the caller says nothing about it — an empty `notable` must never read
    as "nothing important is downstream".
    """
    try:
        ranked = criticality.rank(top=max(top_critical, 1))
    except Exception:  # noqa: BLE001 — an unavailable ranking must not take down the report
        return []
    if not (ranked or {}).get("ok"):
        return []
    by_repo = {row["repo"]: row for row in ranked.get("top") or [] if row.get("repo")}
    out = []
    for item in items:
        row = by_repo.get(item["repo"])
        if not row:
            continue
        # Carry the per-axis rank, not just the combined one: the module this comes from exists
        # precisely because a build-time hub and a runtime traffic hub are different kinds of
        # critical, and collapsing them back to one number here would undo that.
        axes = {name: axis.get("rank") for name, axis in (row.get("axes") or {}).items()
                if isinstance(axis, dict) and axis.get("rank")}
        out.append({
            "repo": item["repo"],
            "relation": item.get("relation") or "transitive",
            "rank": row.get("overall_rank"),
            "axes": axes,
        })
    return sorted(out, key=lambda entry: (entry.get("rank") or 10**6, entry["repo"]))


def _reading(repo, total, direct_count, is_hub, share, unknown_channel,
             unknown_kinds=None, scope_known=False):
    """The sentence that tells a reader what the number is and is not.

    Written as prose because the number alone has repeatedly been misread — a big count reads as
    "big impact" when for shared infrastructure it means "this is shared".
    """
    if not total:
        return ("Nothing depends on this repo in the Maven graph. That is a statement about "
                "BUILD-TIME dependency only — a service can still be coupled at runtime through a "
                "topic, which this number does not see.")
    if not is_hub:
        base = (f"{total} repo(s) depend on {repo}. Small enough that the list itself is the "
                "answer — read it directly.")
    else:
        pct = f"{share:.0%}" if share is not None else "a large share"
        base = (
            f"{repo} is SHARED INFRASTRUCTURE: {total} repos ({pct} of the estate) sit downstream. "
            f"That number describes the repo, not your change — it is not a notification list, a "
            f"review list or a test plan. Use the {direct_count} DIRECT dependent(s) as the list of "
            f"teams to talk to, and the channel spread as what could stop working. Enumerating all "
            f"{total} is what makes this answer unusable.")
    if unknown_channel:
        kinds = unknown_kinds or {}
        base += (f" Channel coverage caveat: {unknown_channel} of the {total} downstream repos have "
                 "no channel of their own, so the spread below is a LOWER bound.")
        if not scope_known:
            base += (" No scan-scope file is present, so it cannot be said which of those were "
                     "checked and found clean and which were never looked at — the lower bound "
                     "cannot be tightened without it.")
        else:
            base += (f" Of those: {kinds.get('scanned_clean', 0)} were scanned and nothing was "
                     f"found (a real finding), and {kinds.get('out_of_scope', 0)} were outside the "
                     "scan's scope by design — the second group is NOT evidence of no channel.")
    return base


def render_markdown(summary):
    """Report lines. Leads with the shape verdict, because that is what changes how to read it."""
    if not summary or not summary.get("total"):
        return [f"- {summary.get('reading')}" if summary else "- not computed"]

    lines = [f"- {summary['reading']}"]
    if summary["is_hub"]:
        lines.append(f"- **direct dependents ({summary['direct_count']})** — this is the "
                     "notification/review list:")
        for name in summary["direct"]:
            lines.append(f"  - {name}")
        lines.append(f"- transitive: **{summary['transitive_count']}** more, not listed "
                     "(see the full `downstream` array if you need them)")
    else:
        lines.append(f"- direct: {summary['direct_count']} · transitive: "
                     f"{summary['transitive_count']}")

    if summary["channels"]:
        lines.append("- channels represented downstream (strongest evidence per channel):")
        for item in summary["channels"]:
            label = _RELATION_LABEL.get(item.get("strongest_relation"), "related")
            detail = f"{item['repos']} repo(s), strongest: {label}"
            if item.get("code_backed"):
                detail += f"; {item['code_backed']} backed by code/config evidence"
            lines.append(f"  - **{item['channel'].upper()}** — {detail}")
    if summary["channel_unknown_repos"]:
        kinds = summary.get("channel_unknown_breakdown") or {}
        lines.append(f"- no channel of their own: {summary['channel_unknown_repos']} repo(s) — the "
                     "spread above is a lower bound")
        if not summary.get("scope_known"):
            lines.append("  - scan scope unknown (no scope file): cannot separate *checked and "
                         "clean* from *never checked*")
        else:
            lines.append(f"  - scanned, nothing found: {kinds.get('scanned_clean', 0)}")
            lines.append(f"  - outside the scan scope by design: {kinds.get('out_of_scope', 0)} "
                         "— not evidence of no channel")
    if summary["notable"]:
        lines.append("- independently critical repos in the affected set:")
        for entry in summary["notable"]:
            rank = f"overall #{entry['rank']}" if entry.get("rank") else "ranked"
            axes = ", ".join(f"{name} #{position}"
                             for name, position in sorted(entry.get("axes", {}).items()))
            detail = f"{rank}; {axes}" if axes else rank
            lines.append(f"  - {entry['repo']} ({detail}, {entry['relation']})")
    return lines
