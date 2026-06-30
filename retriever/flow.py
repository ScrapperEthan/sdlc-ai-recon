"""Stitch the three sources into one best-effort, HONEST routing view.

Synchronous call paths come from CodeGraph (the agent calls codegraph_* directly).
This aggregator covers the cross-repo + async parts CodeGraph can't see, and
always marks where the trail goes partial instead of inventing a hop."""
from . import messages


def trace(use_case_id=None, destination=None):
    out = {"query": {"use_case_id": use_case_id, "destination": destination},
           "steps": [], "partial": []}

    if use_case_id:
        uc = messages.usecase_route(use_case_id=use_case_id)
        if uc.get("available"):
            out["steps"].append({"resolve": "use_case -> topic", "via": uc["source"],
                                 "matches": uc["matches"]})
            for m in uc["matches"]:
                topic = m["topic"]
                if topic:
                    out["steps"].append({"topic": topic,
                                         "consumers": messages.who_consumes(topic)})
        else:
            out["partial"].append(uc["note"])

    if destination:
        out["steps"].append({
            "destination": destination,
            "producers": messages.who_produces(destination),
            "consumers": messages.who_consumes(destination),
        })

    if not out["steps"]:
        out["partial"].append("no input — pass use_case_id and/or destination")
    return out
