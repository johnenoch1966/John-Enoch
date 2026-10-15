"""Function tools exposed to the agents."""

from .evidence import (
    get_unmapped_claims,
    record_evidence,
    record_root_cause_mappings,
    validate_citations,
)
from .scope import save_use_case_catalog
from .taxonomy import lookup_taxonomy
from .web import fetch_page

__all__ = [
    "fetch_page",
    "get_unmapped_claims",
    "lookup_taxonomy",
    "record_evidence",
    "record_root_cause_mappings",
    "save_use_case_catalog",
    "validate_citations",
]
