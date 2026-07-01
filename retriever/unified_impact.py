"""Unified blast-radius view across deps, async messages, and code evidence."""
import shutil
import subprocess

from . import code, config, graph, messages


def _message_peers(seed):
    peers = []
    for edge in messages.routes_for_repo(seed):
        producer = edge.get("producer_repo") or ""
        consumer = edge.get("consumer_repo") or ""
        if producer == seed and consumer:
            direction = "produces_to_consumer"
            peer = consumer
        elif consumer == seed and producer:
            direction = "consumes_from_producer"
            peer = producer
        else:
            direction = "message_edge"
            peer = producer or consumer
        peers.append(
            {
                "direction": direction,
                "peer_repo": peer,
                "destination": edge.get("destination") or "",
                "routing_source": edge.get("routing_source") or "",
                "evidence": edge.get("evidence") or config.MESSAGE_EDGES_CSV,
            }
        )
    return peers


def _call_graph(seed):
    cg = shutil.which("codegraph")
    if not cg:
        return {
            "available": False,
            "note": "codegraph CLI not on PATH; lexical source hits are included instead",
            "hits": code.search_code(seed, "*.java", 20),
        }
    try:
        result = subprocess.run(
            [cg, "explore", seed],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return {
            "available": result.returncode == 0,
            "returncode": result.returncode,
            "output": result.stdout[:8000],
            "error": result.stderr[:2000],
            "hits": [] if result.returncode == 0 else code.search_code(seed, "*.java", 20),
        }
    except Exception as error:  # noqa: BLE001
        return {"available": False, "error": str(error), "hits": code.search_code(seed, "*.java", 20)}


def query(seed, transitive=False):
    """Return deps + async peers + callers/source hits for a repo or symbol."""
    seed = (seed or "").strip()
    if not seed:
        return {"error": "seed is required"}

    dep = graph.impact(seed, transitive=transitive)
    return {
        "seed": seed,
        "dependency_edges": {
            "source": config.EDGES_CSV,
            "mode": dep["mode"],
            "depended_on_by": dep["depended_on_by"],
            "depends_on": dep["depends_on"],
        },
        "message_edges": {
            "source": config.MESSAGE_EDGES_CSV,
            "peers": _message_peers(seed),
        },
        "callers": _call_graph(seed),
    }
