"""Thin facade over `retriever/usecase_catalog.py` (Round A: manifest-driven, environment-aware
Use Case dataset). Kept so `impact_report.py`, `outage_report.py`, `webapp/tools.py`,
`mcp_server.py`, and `retrieval_service.py` need no import churn — every name below is a direct
re-export; see `usecase_catalog.py` for the real implementation and docstrings."""
from .usecase_catalog import (  # noqa: F401
    BUSINESS_CATEGORY_ENUM,
    canonicalize_source_system,
    channels_for_use_case,
    consent_preflight,
    ext_by_use_case_id,
    is_stale,
    master_for,
    owners_for,
    quality_report,
    render_quality_markdown,
    resolve_column,
    resolve_endpoint,
    route_dimension,
    rules_by_use_case_id,
    search_usecases,
    snapshot_manifest,
    source_system_coverage,
    source_systems,
    use_cases_for_source_system,
)
