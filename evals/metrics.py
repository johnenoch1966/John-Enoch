"""Scoring functions for the evaluation suite (pure, deterministic)."""

from __future__ import annotations

from statistics import mean
from typing import Any

from adoption_agent import config
from adoption_agent.privacy import count_placeholders, redact_regex
from adoption_agent.schemas import IndustrySynthesis, SegmentSynthesis
from adoption_agent.tools.taxonomy import lookup_taxonomy


def _avg(values: list[float]) -> float:
    return round(mean(values), 4) if values else 0.0


# --------------------------------------------------------------------------
# Root-cause / industry tagging (semantic-bias agent)
# --------------------------------------------------------------------------
def _norm_industries(values: list[str]) -> set[str]:
    s = set(values or [])
    specific = s - {"cross_industry"}
    return specific or {"cross_industry"}


def root_cause_scores(cases: list[dict], predictions: dict[str, dict]) -> dict[str, float]:
    """Compare predicted mappings to golden labels.

    top1_accuracy   - primary predicted root cause is an acceptable label
    any_accuracy    - any predicted root cause is an acceptable label
    industry_match  - predicted industries overlap the golden industries
    coverage        - share of golden cases the agent mapped at all
    """
    top1, anyhit, ind, covered = [], [], [], []
    for case in cases:
        pred = predictions.get(case["id"])
        covered.append(float(bool(pred and pred.get("root_cause_ids"))))
        if not pred or not pred.get("root_cause_ids"):
            top1.append(0.0)
            anyhit.append(0.0)
            ind.append(0.0)
            continue
        expected = set(case["expected_root_causes"])
        roots = pred["root_cause_ids"]
        top1.append(float(roots[0] in expected))
        anyhit.append(float(bool(set(roots) & expected)))
        p_ind = _norm_industries(pred.get("industries", []))
        e_ind = set(case["expected_industries"])
        ind.append(float(bool(p_ind & e_ind)))
    return {
        "top1_accuracy": _avg(top1),
        "any_accuracy": _avg(anyhit),
        "industry_match": _avg(ind),
        "coverage": _avg(covered),
        "n": len(cases),
    }


def taxonomy_lookup_scores(cases: list[dict]) -> dict[str, float]:
    """How often the deterministic lookup tool suggests an acceptable cause."""
    top1, top3 = [], []
    for case in cases:
        cands = [c["id"] for c in lookup_taxonomy(case["stated_factor"])["candidates"]]
        expected = set(case["expected_root_causes"])
        top1.append(float(bool(cands) and cands[0] in expected))
        top3.append(float(bool(set(cands[:3]) & expected)))
    return {"top1_hit": _avg(top1), "top3_hit": _avg(top3), "n": len(cases)}


# --------------------------------------------------------------------------
# PII masking
# --------------------------------------------------------------------------
def pii_scores(cases: list[dict]) -> dict[str, float]:
    """recall: expected PII items masked; false_positive_rate: clean texts altered;
    keep_rate: research facts (numbers, names of standards) preserved."""
    recall, fp, keep = [], [], []
    for case in cases:
        masked, counts = redact_regex(case["text"])
        expected = case["expected_types"]
        found = count_placeholders(masked)
        if expected:
            need: dict[str, int] = {}
            for t in expected:
                need[t] = need.get(t, 0) + 1
            hits = sum(min(found.get(t, 0), n) for t, n in need.items())
            recall.append(hits / len(expected))
        else:
            fp.append(float(bool(counts)))
        keep.extend(float(k in masked) for k in case.get("must_keep", []))
    return {
        "recall": _avg(recall),
        "false_positive_rate": _avg(fp),
        "keep_rate": _avg(keep),
        "n": len(cases),
    }


# --------------------------------------------------------------------------
# Synthesis rubric (segment + industry analysts)
# --------------------------------------------------------------------------
_LIMITS = {  # field -> max words (schema limit + ~30% tolerance)
    "factor": 10,
    "approach": 13,
    "point": 13,
    "action": 16,
    "headline": 32,
    "explanation": 26,
    "rationale": 26,
    "detail": 26,
    "value_lever": 13,
    "key_blocker": 13,
}


def _walk_text_fields(obj: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k in _LIMITS:
                out.append((k, v))
            else:
                _walk_text_fields(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_text_fields(v, out)


def _cited_ids(obj: Any) -> list[int]:
    ids: list[int] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "evidence_ids" and isinstance(v, list):
                ids.extend(int(i) for i in v)
            else:
                ids.extend(_cited_ids(v))
    elif isinstance(obj, list):
        for v in obj:
            ids.extend(_cited_ids(v))
    return ids


def segment_rubric(synthesis: dict, valid_ids: set[int], catalog: dict | None) -> dict[str, float]:
    try:
        SegmentSynthesis.model_validate(synthesis)
        schema_ok = 1.0
    except Exception:
        schema_ok = 0.0
    segs = {s.get("segment"): s for s in synthesis.get("segments", [])}
    seg_cov = _avg([float(s in segs) for s in ("copilot", "workflow")])

    uc_cov, counts = [], []
    for seg_name, seg in segs.items():
        cat_ids = {
            u["id"] for u in (catalog or {}).get("use_cases", []) if u["segment"] == seg_name
        }
        ranked = {u.get("use_case_id") for u in seg.get("use_case_impact", [])}
        if cat_ids:
            uc_cov.append(len(ranked & cat_ids) / len(cat_ids))
        counts.append(float(len(seg.get("top_drivers", [])) >= 4))
        counts.append(float(len(seg.get("top_inhibitors", [])) >= 4))
        counts.append(float(len(seg.get("high_adoption_approaches", [])) >= 3))
        counts.append(float(len(seg.get("low_adoption_patterns", [])) >= 3))

    cited = _cited_ids(synthesis)
    fields: list[tuple[str, str]] = []
    _walk_text_fields(synthesis, fields)
    return {
        "schema_valid": schema_ok,
        "segment_coverage": seg_cov,
        "use_case_coverage": _avg(uc_cov),
        "section_completeness": _avg(counts),
        "citation_validity": _avg([float(i in valid_ids) for i in cited]) if cited else 0.0,
        "items_cited": _avg(
            [
                float(bool(x.get("evidence_ids")))
                for s in segs.values()
                for x in s.get("top_drivers", []) + s.get("top_inhibitors", [])
            ]
        ),
        "conciseness": _avg([float(len(v.split()) <= _LIMITS[k]) for k, v in fields]),
    }


def industry_rubric(synthesis: dict, valid_ids: set[int]) -> dict[str, float]:
    try:
        IndustrySynthesis.model_validate({"industries": synthesis.get("industries", [])})
        schema_ok = 1.0
    except Exception:
        schema_ok = 0.0
    profiles = {p.get("industry_id"): p for p in synthesis.get("industries", [])}
    complete = [
        float(
            ind in profiles
            and len(profiles[ind].get("drivers", [])) == 5
            and len(profiles[ind].get("blockers", [])) == 5
            and len(profiles[ind].get("advice", [])) == 5
        )
        for ind in config.INDUSTRIES
    ]
    cited = _cited_ids(synthesis.get("industries", []))
    fields: list[tuple[str, str]] = []
    _walk_text_fields(synthesis.get("industries", []), fields)
    return {
        "schema_valid": schema_ok,
        "industry_completeness": _avg(complete),
        "citation_validity": _avg([float(i in valid_ids) for i in cited]) if cited else 0.0,
        "conciseness": _avg([float(len(v.split()) <= _LIMITS[k]) for k, v in fields]),
    }
