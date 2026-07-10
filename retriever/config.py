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
BUNDLES_JSON = _p("SDLC_BUNDLES", "index", "bundles.json")
GLOSSARY_JSON = _p("SDLC_GLOSSARY", "index", "glossary.json")
REPO_TAGS_JSON = _p("SDLC_REPO_TAGS", "index", "repo_tags.json")
REPO_TAGS_OVERRIDE_JSON = _p("SDLC_REPO_TAGS_OVERRIDE", "index", "repo_tags.override.json")
MDC_SHEET_XLSX = _p("SDLC_MDC_SHEET", "MDC_Repo_List_Analysis.xlsx")
REPO_TAGS_MDC_JSON = _p("SDLC_REPO_TAGS_MDC", "index", "repo_tags.mdc.json")
TAG_RECONCILE_MD = _p("SDLC_TAG_RECONCILE_MD", "index", "reports", "TAG_RECONCILE.md")
TAG_RECONCILE_JSON = _p("SDLC_TAG_RECONCILE_JSON", "index", "reports", "TAG_RECONCILE.json")
DELIVERY_TOPOLOGY_JSON = _p("SDLC_DELIVERY_TOPOLOGY", "index", "delivery_topology.json")
DELIVERY_TOPOLOGY_OVERRIDE_JSON = _p(
    "SDLC_DELIVERY_TOPOLOGY_OVERRIDE", "index", "delivery_topology.override.json"
)
