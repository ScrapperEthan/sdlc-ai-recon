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
out loud, because repo_tags channel coverage has been measured at ~35% and quietly reporting
"3 channels affected" out of a set where two thirds have no channel tag would be a confident
undercount. That is the same failure this codebase keeps closing: silence reading as absence.
"""
from . import criticality, graph, repo_tags

# A repo reaching this share of the estate is telling you about ITSELF (it is shared), not about the
# change you asked about. Not a magic constant so much as the point where enumerating stops helping:
# a fifth of the estate cannot be a notification list, a review list, or a test plan.
HUB_SHARE = 0.20
# Below this the full list is genuinely the answer, so no summary shape-change is warranted even if
# the share calculation is unavailable (tiny or missing estate).
SMALL_ENOUGH_TO_LIST = 25

_UNKNOWN = "unknown"


def _channels_of(entry):
    """The repo's OWN channels: structural tokens plus the business sheet's declaration.

    `serves_channels` is deliberately excluded — it is itself computed from graph blast radius, so
    folding it in here would let a repo inherit a channel from the very relationship being measured
    and report it back as independent evidence.
    """
    channels = {c.strip().lower() for c in entry.get("channel") or [] if c.strip()}
    channels.update(c.strip().lower() for c in entry.get("channel_declared") or [] if c.strip())
    return channels


def summarise(repo, downstream, tags=None, top_critical=10):
    """Blast radius for one repo -> a shape verdict plus the three things worth acting on.

    `downstream` is the report's existing list of `{repo, relation, ...}` items; nothing is removed
    from it. Returns a dict that is safe to render even when repo_tags or the criticality inputs are
    absent (both degrade to "unknown", never to a smaller-looking number).
    """
    tags = repo_tags.load() if tags is None else tags
    items = [item for item in downstream or [] if isinstance(item, dict) and item.get("repo")]
    direct = [item["repo"] for item in items if item.get("relation") == "direct"]
    transitive = [item["repo"] for item in items if item.get("relation") != "direct"]
    total = len(items)

    estate = len(graph.known_repos() or ()) or len(tags) or 0
    share = (total / estate) if estate else None
    is_hub = bool(share is not None and share >= HUB_SHARE and total > SMALL_ENOUGH_TO_LIST)

    by_channel = {}
    unknown_channel = 0
    for item in items:
        channels = _channels_of(repo_tags.for_repo(item["repo"], tags))
        if not channels:
            unknown_channel += 1
            continue
        for channel in channels:
            by_channel[channel] = by_channel.get(channel, 0) + 1

    return {
        "repo": repo,
        "total": total,
        "direct": sorted(direct),
        "direct_count": len(direct),
        "transitive_count": len(transitive),
        "estate": estate,
        "share_of_estate": round(share, 4) if share is not None else None,
        "is_hub": is_hub,
        "channels": [{"channel": name, "repos": count}
                     for name, count in sorted(by_channel.items(), key=lambda kv: (-kv[1], kv[0]))],
        "channel_unknown_repos": unknown_channel,
        "notable": _notable(items, top_critical),
        "reading": _reading(repo, total, len(direct), is_hub, share, unknown_channel),
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


def _reading(repo, total, direct_count, is_hub, share, unknown_channel):
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
        base += (f" Channel coverage caveat: {unknown_channel} of the {total} downstream repos have "
                 "no channel tag, so the channel spread below is a LOWER bound, not the full "
                 "picture.")
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
        spread = ", ".join(f"{item['channel'].upper()} ({item['repos']})"
                           for item in summary["channels"])
        lines.append(f"- channels represented downstream: {spread}")
    if summary["channel_unknown_repos"]:
        lines.append(f"- no channel tag: {summary['channel_unknown_repos']} repo(s) — the spread "
                     "above is a lower bound")
    if summary["notable"]:
        lines.append("- independently critical repos in the affected set:")
        for entry in summary["notable"]:
            rank = f"overall #{entry['rank']}" if entry.get("rank") else "ranked"
            axes = ", ".join(f"{name} #{position}"
                             for name, position in sorted(entry.get("axes", {}).items()))
            detail = f"{rank}; {axes}" if axes else rank
            lines.append(f"  - {entry['repo']} ({detail}, {entry['relation']})")
    return lines
