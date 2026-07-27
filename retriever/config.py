"""Paths for the retrieval layer. Override any of them with env vars."""
import os

ROOT = os.environ.get("SDLC_ROOT", os.getcwd())


def _p(env, *parts):
    return os.environ.get(env) or os.path.join(ROOT, *parts)


def _cfg(env, name, *legacy_parts):
    """Knob files (column maps + vocabulary) live in the COMMITTED ``config/`` dir — unlike
    ``index/``, which is gitignored, so an intranet-Codex edit there can actually be versioned and
    pushed to the internal repo instead of dying on the box (see AGENTS.md §4).

    Resolution order: env override -> ``config/<name>`` -> a pre-existing legacy path. The legacy
    fallback only applies when that file already exists, so a box that still holds an older
    ``index/`` copy keeps working until it is moved, while a clean checkout uses ``config/``.
    """
    override = os.environ.get(env)
    if override:
        return override
    new_path = os.path.join(ROOT, "config", name)
    if os.path.exists(new_path) or not legacy_parts:
        return new_path
    legacy = os.path.join(ROOT, *legacy_parts)
    return legacy if os.path.exists(legacy) else new_path


MIRROR = _p("SDLC_MIRROR", "mirror")
RECON_DIR = _p("SDLC_RECON", "recon_out")
INDEX_DIR = _p("SDLC_INDEX", "index")

EDGES_CSV = _p("SDLC_EDGES", "recon_out", "internal_edges.csv")
# Full scanned repo list from recon_maven_graph — seeds the tag universe so repos with no
# internal Maven edge still get an entry (edge endpoints alone miss config/Gradle/isolated repos).
REPOS_TXT = _p("SDLC_REPOS_TXT", "recon_out", "repos.txt")
MESSAGE_EDGES_CSV = _p("SDLC_MSG_EDGES", "index", "message_edges.csv")
MESSAGE_CHANNELS_JSON = _p("SDLC_MSG_CHANNELS", "index", "message_channels.json")
USECASE_SNAPSHOT_CSV = _p(
    "SDLC_USECASE_SNAPSHOT", "index", "tbl_event_router_usecase_topic.snapshot.csv"
)
# Use Case master data (Tier 0) — second snapshot on the same use_case_id primary key; supplies
# identity/governance + the upstream source_system that the routing snapshot above doesn't carry.
USECASE_MASTER_CSV = _p("SDLC_USECASE_MASTER", "index", "tbl_use_case.snapshot.csv")
SOURCE_SYSTEM_ALIASES_JSON = _cfg(
    "SDLC_SOURCE_SYSTEM_ALIASES", "source_system_aliases.json", "index", "source_system_aliases.json"
)
# Round A (UAT catalog): manifest-driven, environment-aware dataset directory. A single "active"
# dataset carries all three UAT tables (tbl_use_case / tbl_use_case_channel_rule / tbl_use_case_ext)
# plus an optional same-environment route snapshot. Legacy single-file USECASE_MASTER_CSV above stays
# as a back-compat fallback when no manifest dir exists (see usecase_catalog.active_dataset()).
USECASE_DATASET_DIR = _p("SDLC_USECASE_DATASET", "index", "usecase-snapshots", "active")
# Round B2: owner-confirmed rule_text operator semantics. Missing/default -> every operator
# "unconfirmed" (see retriever/rule_text.py) — the single seam an owner answer plugs into.
# CONFIRMED 2026-07-27, so this now ships as a committed config/ file rather than a box-local blank.
RULE_TEXT_SEMANTICS_JSON = _cfg(
    "SDLC_RULE_TEXT_SEMANTICS", "rule_text_semantics.json", "index", "rule_text_semantics.json"
)
BUNDLES_JSON = _p("SDLC_BUNDLES", "index", "bundles.json")
# Per-bundle CodeGraph indexes: staging roots live under CODEGRAPH_ROOT/<bundle>/ and the
# build manifest records what got indexed (see build_codegraph.py).
CODEGRAPH_ROOT = _p("SDLC_CODEGRAPH_ROOT", "index", "codegraph")
CODEGRAPH_BUILD_JSON = _p("SDLC_CODEGRAPH_BUILD", "index", "codegraph_build.json")
GLOSSARY_JSON = _p("SDLC_GLOSSARY", "index", "glossary.json")
REPO_TAGS_JSON = _p("SDLC_REPO_TAGS", "index", "repo_tags.json")
REPO_TAGS_OVERRIDE_JSON = _p("SDLC_REPO_TAGS_OVERRIDE", "index", "repo_tags.override.json")
MDC_SHEET_XLSX = _p("SDLC_MDC_SHEET", "MDC_Repo_List_Analysis.xlsx")
REPO_TAGS_MDC_JSON = _p("SDLC_REPO_TAGS_MDC", "index", "repo_tags.mdc.json")
# Codex-editable column map for the MDC sheet. Missing -> enrich_repo_tags.DEFAULT_SCHEMA
# (the v0.2 layout) is used, so behaviour is unchanged until someone supplies a schema.
MDC_SHEET_SCHEMA_JSON = _p("SDLC_MDC_SHEET_SCHEMA", "mdc_sheet_schema.json")
# Authoritative in-scope MDC roster (every repo the sheet lists = MDC). The consumption
# layer scopes list_repos/search to this set; amet-* / anything not in the sheet is out-of-scope.
MDC_ROSTER_JSON = _p("SDLC_MDC_ROSTER", "index", "mdc_roster.json")
TAG_RECONCILE_MD = _p("SDLC_TAG_RECONCILE_MD", "index", "reports", "TAG_RECONCILE.md")
TAG_RECONCILE_JSON = _p("SDLC_TAG_RECONCILE_JSON", "index", "reports", "TAG_RECONCILE.json")
DELIVERY_TOPOLOGY_JSON = _p("SDLC_DELIVERY_TOPOLOGY", "index", "delivery_topology.json")
DELIVERY_TOPOLOGY_OVERRIDE_JSON = _p(
    "SDLC_DELIVERY_TOPOLOGY_OVERRIDE", "index", "delivery_topology.override.json"
)
# Architecture map: static node catalog (committed drawing skeleton, no repo names) plus
# the generated node->repo binding and an optional hand override for non-name-revealing nodes.
ARCH_NODES_JSON = _p("SDLC_ARCH_NODES", "static", "arch_nodes.json")
ARCH_MAP_JSON = _p("SDLC_ARCH_MAP", "index", "arch_map.json")
ARCH_MAP_OVERRIDE_JSON = _p("SDLC_ARCH_MAP_OVERRIDE", "index", "arch_map.override.json")
