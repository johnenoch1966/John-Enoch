"""Canonical industry helpers."""

from __future__ import annotations

from typing import Any

from . import config

# Keyword -> canonical industry id. Used to normalise free-text industries
# (e.g. "Retail Banking", "Pharma") from older catalogs or model output.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fsi": (
        "bank",
        "financ",
        "insur",
        "fintech",
        "wealth",
        "payment",
        "lending",
        "credit",
        "capital market",
        "investment",
        "reinsur",
        "remittance",
        "crypto",
        "asset manag",
    ),
    "hcls": ("health", "pharma", "life science", "hospital", "medical", "biotech", "payer"),
    "manufacturing": (
        "manufactur",
        "automotive",
        "industrial",
        "aerospace",
        "defense",
        "defence",
        "electronics",
        "semiconductor",
        "chemical",
    ),
    "retail_cpg": (
        "retail",
        "consumer",
        "e-commerce",
        "ecommerce",
        "cpg",
        "hospitality",
        "airline",
        "travel",
        "logistics",
        "supply chain",
    ),
    "tmt": (
        "telecom",
        "media",
        "technology",
        "software",
        "saas",
        "it services",
        "cloud",
        "gaming",
        "digital",
    ),
    "public_sector": ("government", "public sector", "federal", "defense agency", "education"),
    "energy_utilities": ("energy", "utilit", "oil", "gas", "power", "critical infrastructure"),
}


def canonical_industries(labels: list[str]) -> list[str]:
    """Map free-text industry labels to canonical ids (order-preserving, unique).

    Each label maps to its FIRST matching industry, in the priority order of
    `_KEYWORDS` (so "Retail Banking" is FSI, not Retail).
    """
    out: list[str] = []
    for label in labels:
        low = label.lower()
        for ind, kws in _KEYWORDS.items():
            if any(k in low for k in kws):
                if ind not in out:
                    out.append(ind)
                break
    return out


def use_case_industries(use_case: dict[str, Any]) -> list[str]:
    """Canonical industries of a catalog use case (explicit tags win)."""
    explicit = [i for i in use_case.get("industries") or [] if i in config.INDUSTRIES]
    return explicit or canonical_industries(use_case.get("example_industries") or [])


def industries_brief() -> str:
    return "\n".join(f"- {k}: {v}" for k, v in config.INDUSTRIES.items())
