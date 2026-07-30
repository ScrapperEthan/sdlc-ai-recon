"""Which repos are actually critical — scored on several independent axes, not one ranking.

Showcase 8 asks for a "core components top 10". The tempting answer is `graph.hubs()`: sort by Maven
dependents, take ten, done. That answer is wrong in a way that is hard to see, because a build-time
dependency hub and a runtime traffic hub are different things. `api-common` is depended on by
everything and carries no messages; a delivery job carries every SMS in the bank and is depended on
by nothing. A single list silently picks one definition of "critical" and hides the choice.

So this scores each axis separately, reports the per-axis rank, and — the part that matters — states
which axes it CANNOT score. Three of the seven dimensions the business would want need data we do not
have (production traffic, incident history, test coverage). Leaving them out silently would produce a
confident top-10 that is really a top-10-of-what-we-happened-to-measure.

## Scored here

| axis | source | what it means |
| --- | --- | --- |
| `dependency` | `recon_out/internal_edges.csv` | how many repos break if this one changes |
| `message` | `index/message_edges.csv` | how many async topics it produces or consumes |
| `business` | message topics -> use-case route snapshot | how much business traffic is routed through it |

## Deliberately NOT scored, and why that is stated in every result

* **production traffic** — CloudWatch has it (per-resource metrics), but it is not joined to repos
  and a 460-repo sweep at ~26s per call is not a query anyone will run. Needs the alarm/resource ->
  repo join, then a batch job.
* **incident history** — lives in ServiceNow / the AIOps alert store, neither of which we read.
* **test coverage** — CI JaCoCo/Surefire reports, not in the mirror.

A weighted total is offered, but it is explicitly a *view* over the axes that exist, and the missing
axes travel with it. Anyone quoting a rank without the caveats is quoting a partial model.
"""
import collections

from . import graph, messages, repo_tags

# Equal weights, deliberately. Any other split would be an unexamined claim about which axis matters
# more, and nobody has made that call — the per-axis ranks are the real output.
DEFAULT_WEIGHTS = {"dependency": 1.0, "message": 1.0, "business": 1.0}

MISSING_DIMENSIONS = [
    {"dimension": "production_traffic",
     "why_missing": "CloudWatch holds per-resource metrics, but resource->repo is not joined and a "
                    "460-repo sweep at ~26s per call is not a runnable query",
     "what_would_fix_it": "the alarm/resource -> repo mapping plus a batch metric collector",
     "blocked_on": "platform team (resource inventory) + our own batch job"},
    {"dimension": "incident_history",
     "why_missing": "incident records live in ServiceNow / the AIOps alert store, which we do not read",
     "what_would_fix_it": "an export of incidents per service for a fixed window",
     "blocked_on": "the incident-management owner"},
    {"dimension": "test_coverage",
     "why_missing": "coverage lives in CI reports (JaCoCo/Surefire), not in the source mirror",
     "what_would_fix_it": "jacoco.xml per repo from the CI archive",
     "blocked_on": "CI/CD owner"},
]


def _dependency_scores():
    """{repo: how many repos depend on it}. Direct dependents, not transitive.

    Direct on purpose: transitive closure over a 460-repo Maven graph makes almost everything look
    critical, which tells you nothing about where a change is genuinely dangerous.
    """
    try:
        _forward, reverse = graph.load_dependency_graph()
    except Exception:                                    # noqa: BLE001 — index absent is normal
        return {}, "recon_out/internal_edges.csv not available"
    return {repo: len(dependents) for repo, dependents in reverse.items()}, ""


def _message_scores():
    """{repo: distinct topics it produces or consumes}."""
    counts = collections.defaultdict(set)
    try:
        for repo in graph.known_repos() or ():
            for edge in messages.routes_for_repo(repo) or ():
                destination = (edge.get("destination") or "").strip()
                if destination:
                    counts[repo].add(destination)
    except Exception:                                    # noqa: BLE001
        return {}, "index/message_edges.csv not available"
    return {repo: len(topics) for repo, topics in counts.items()}, ""


def _business_scores(topic_use_cases):
    """{repo: use cases routed onto the topics it touches}.

    Uses the same dev/SCT route snapshot as everything else, so it inherits the same caveat: the
    snapshot covers far fewer topics than the message graph, and a zero here means "not covered by
    the snapshot", never "no business impact".
    """
    return {repo: len({uc for use_cases in topics.values() for uc in use_cases})
            for repo, topics in topic_use_cases.items()}


def _topics_per_repo():
    """{repo: {topic: [use_case, …]}}.

    The use-case lookup is cached across repos, not per repo: topics are shared, and re-scanning the
    route snapshot once per (repo, topic) pair instead of once per topic is the difference between a
    second and a minute on a 460-repo estate.
    """
    out = {}
    cache = {}
    try:
        repos = graph.known_repos() or ()
    except Exception:                                    # noqa: BLE001
        return out
    for repo in repos:
        topics = {}
        for edge in messages.routes_for_repo(repo) or ():
            destination = (edge.get("destination") or "").strip()
            if not destination or destination in topics:
                continue
            if destination not in cache:
                try:
                    found = messages.reverse_lookup_use_cases(destination) or {}
                except Exception:                        # noqa: BLE001
                    found = {}
                items = found.get("items") if isinstance(found, dict) else None
                cache[destination] = [
                    item["use_case"] for item in (items or [])
                    if isinstance(item, dict) and item.get("use_case")]
            topics[destination] = cache[destination]
        if topics:
            out[repo] = topics
    return out


def _ranked(scores):
    """{repo: rank}, 1 = highest. Ties share the better rank, so a tie is not broken arbitrarily."""
    ordered = sorted({value for value in scores.values()}, reverse=True)
    position = {}
    seen = 0
    for value in ordered:
        position[value] = seen + 1
        seen += sum(1 for v in scores.values() if v == value)
    return {repo: position[value] for repo, value in scores.items()}


def rank(top=10, weights=None):
    """Score every known repo on the axes we can measure. Returns ranks plus what is missing."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    dependency, dep_error = _dependency_scores()
    message, msg_error = _message_scores()
    topic_use_cases = _topics_per_repo()
    business = _business_scores(topic_use_cases)

    axes = {"dependency": dependency, "message": message, "business": business}
    available = {name: scores for name, scores in axes.items() if scores}
    if not available:
        return {
            "ok": False,
            "error": "no scoring axis is available — the dependency and message indexes are both "
                     "missing. Run the recon build on the box.",
            "axis_errors": {"dependency": dep_error, "message": msg_error},
            "missing_dimensions": MISSING_DIMENSIONS,
        }

    # Normalise per axis before combining: raw dependent counts and raw topic counts are not the same
    # unit, and adding them would let whichever axis happens to have bigger numbers dominate.
    maxima = {name: max(scores.values()) or 1 for name, scores in available.items()}
    ranks = {name: _ranked(scores) for name, scores in available.items()}

    repos = sorted(set().union(*(set(scores) for scores in available.values())))
    rows = []
    for repo in repos:
        per_axis = {}
        total = 0.0
        for name, scores in available.items():
            value = scores.get(repo, 0)
            normalised = value / maxima[name]
            total += normalised * weights.get(name, 1.0)
            per_axis[name] = {"value": value, "normalised": round(normalised, 4),
                              "rank": ranks[name].get(repo)}
        rows.append({"repo": repo, "score": round(total, 4), "axes": per_axis,
                     "channels": sorted(repo_tags.channels_for_repo(repo) or [])})

    rows.sort(key=lambda row: (-row["score"], row["repo"]))
    for position, row in enumerate(rows[:top], 1):
        row["overall_rank"] = position

    return {
        "ok": True,
        "top": rows[:top],
        "scored_repos": len(rows),
        "axes_scored": sorted(available),
        "axis_errors": {name: error for name, error in
                        (("dependency", dep_error), ("message", msg_error)) if error},
        "weights": {name: weights.get(name, 1.0) for name in available},
        "missing_dimensions": MISSING_DIMENSIONS,
        "how_to_read": (
            "Per-axis ranks are the real output; `score` is one equal-weighted view over the axes "
            "that exist. A build-time dependency hub and a runtime traffic hub are different kinds "
            "of critical — compare the axes rather than quoting the combined rank alone."),
        "caveats": [
            "THREE of the dimensions the business would want are not scored at all: production "
            "traffic, incident history, test coverage. See `missing_dimensions`. A top-10 from this "
            "model is a top-10 of what is measurable today, and must be presented that way.",
            "`business` uses the dev/SCT use-case route snapshot, which covers far fewer topics than "
            "the message graph. A zero means 'not covered by that snapshot', never 'no business "
            "impact'.",
            "`dependency` counts DIRECT dependents. Transitive closure over this graph makes almost "
            "everything look critical.",
        ],
    }
