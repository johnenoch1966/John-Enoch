"""Root-cause taxonomy loader and lookup tool (semantic-bias analysis)."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import yaml

from .. import config


@lru_cache(maxsize=1)
def load_taxonomy() -> list[dict[str, Any]]:
    with open(config.TAXONOMY_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)["categories"]


def taxonomy_ids() -> set[str]:
    return {c["id"] for c in load_taxonomy()}


def taxonomy_names() -> dict[str, str]:
    return {c["id"]: c["name"] for c in load_taxonomy()}


def taxonomy_brief() -> str:
    """One line per category, for embedding in prompts."""
    return "\n".join(f"- {c['id']}: {c['name']} - {c['description']}" for c in load_taxonomy())


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def lookup_taxonomy(stated_factor: str) -> dict:
    """Suggests underlying root-cause categories for a factor as stated in a report.

    Scores each taxonomy category by overlap between the stated factor and the
    category's signal phrases, and returns the best candidates together with
    worked inference examples. Use the result as a prompt for your own
    judgement, not as the final answer: surface wording often hides a
    different root cause (e.g. "connectors not enabled" -> Risk & Auditability).

    Args:
      stated_factor: The adoption driver or inhibitor exactly as the source phrases it.

    Returns:
      dict with 'candidates' (ranked list of {id, name, score, matched_signals,
      inference_examples}) and 'all_category_ids'.
    """
    text = stated_factor.lower()
    words = _tokens(stated_factor)
    scored = []
    for cat in load_taxonomy():
        matched = []
        score = 0.0
        for sig in cat.get("signals", []):
            if sig in text:
                matched.append(sig)
                score += 2.0
            else:
                overlap = len(_tokens(sig) & words)
                if overlap:
                    score += 0.5 * overlap / max(len(_tokens(sig)), 1)
        if score > 0:
            scored.append(
                {
                    "id": cat["id"],
                    "name": cat["name"],
                    "score": round(score, 2),
                    "matched_signals": matched,
                    "inference_examples": cat.get("inference_examples", []),
                }
            )
    scored.sort(key=lambda c: -c["score"])
    return {"candidates": scored[:3], "all_category_ids": sorted(taxonomy_ids())}
