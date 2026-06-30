"""Paths for the retrieval layer. Override any of them with env vars."""
import os

ROOT = os.environ.get("SDLC_ROOT", os.getcwd())


def _p(env, *parts):
    return os.environ.get(env) or os.path.join(ROOT, *parts)


MIRROR = _p("SDLC_MIRROR", "mirror")
RECON_DIR = _p("SDLC_RECON", "recon_out")
INDEX_DIR = _p("SDLC_INDEX", "index")

EDGES_CSV = _p("SDLC_EDGES", "recon_out", "internal_edges.csv")
MESSAGE_EDGES_CSV = _p("SDLC_MSG_EDGES", "index", "message_edges.csv")
USECASE_SNAPSHOT_CSV = _p(
    "SDLC_USECASE_SNAPSHOT", "index", "tbl_event_router_usecase_topic.snapshot.csv"
)
