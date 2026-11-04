"""Transparent, deterministic scoring of evidence (weights in scoring.yaml).

Scores are computed by code from the evidence base; the AI analysts only
write the labels and explanations. This makes every ranking reproducible and
explainable ("why is this ranked #1?").

Building blocks
---------------
claim weight   w = source_weight(source_type, vendor_affiliated) x recency(published)
                   [x mapping confidence, for root-cause based scores]
weighted evidence over a set of claims
               = sum over DISTINCT SOURCES of the highest claim weight from that
                 source, so one report counts once however many claims it yields

Scores
------
driver / blocker (segment or industry x polarity x root cause)
               = weighted evidence of that group, normalised 0-100 against the
                 strongest group of the same segment/industry and polarity
use case       = 100 x evidence^0.5 x (0.4 + 0.6 x positivity)
                 evidence   = weighted evidence (drivers + blockers) / best in segment
                 positivity = weighted drivers / (weighted drivers + blockers)
strength label = strong >= 66 > moderate >= 33 > weak
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONFIG = Path(__file__).resolve().parent / "scoring.yaml"


@lru_cache(maxsize=1)
def weights() -> dict[str, Any]:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _age_years(published: str | None, now: datetime) -> float | None:
    m = re.search(r"(\d{4})(?:-(\d{1,2}))?", published or "")
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2) or 6)
    return max(0.0, (now.year - year) + (now.month - month) / 12)


def recency_weight(published: str | None, now: datetime | None = None) -> float:
    w = weights()
    age = _age_years(published, now or datetime.now(UTC))
    if age is None:
        return float(w["unknown_date_weight"])
    for band in w["recency"]:
        if age <= band["max_age_years"]:
            return float(band["weight"])
    return float(w["recency"][-1]["weight"])


def source_weight(source_type: str | None, vendor_affiliated: bool) -> float:
    w = weights()
    sw = float(w["source_weights"].get(source_type or "other", w["source_weights"]["other"]))
    return min(sw, float(w["vendor_affiliated_cap"])) if vendor_affiliated else sw


def claim_weight(
    claim: dict[str, Any], *, use_confidence: bool = False, now: datetime | None = None
) -> float:
    wt = source_weight(claim.get("source_type"), bool(claim.get("vendor_affiliated")))
    wt *= recency_weight(claim.get("published"), now)
    if use_confidence:
        wt *= float(weights()["confidence_weights"].get(claim.get("confidence") or "high", 1.0))
    return round(wt, 4)


def weighted_evidence(
    claims: list[dict[str, Any]], *, use_confidence: bool = False, now: datetime | None = None
) -> float:
    """Sum over distinct sources of the best claim weight from each source."""
    best: dict[Any, float] = {}
    for c in claims:
        w = claim_weight(c, use_confidence=use_confidence, now=now)
        key = c.get("source_id", id(c))
        best[key] = max(best.get(key, 0.0), w)
    return round(sum(best.values()), 4)


def strength_label(score: float) -> str:
    bands = weights()["strength_bands"]
    return (
        "strong"
        if score >= bands["strong"]
        else "moderate"
        if score >= bands["moderate"]
        else "weak"
    )


def _breakdown(claims: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = defaultdict(int)
    for c in claims:
        by_type[c.get("source_type") or "other"] += 1
    return {
        "claims": len(claims),
        "sources": len({c.get("source_id") for c in claims}),
        "vendor_share": round(
            sum(bool(c.get("vendor_affiliated")) for c in claims) / len(claims), 2
        )
        if claims
        else 0.0,
        "by_source_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
    }


# ---------------------------------------------------------------------------
# Root-cause group scores (drivers / blockers)
# ---------------------------------------------------------------------------
def group_scores(
    claims: list[dict[str, Any]], key_fn, now: datetime | None = None
) -> dict[tuple, dict[str, Any]]:
    """Score groups keyed by key_fn(claim) -> (scope, polarity, root_cause) or None.

    Scores are normalised 0-100 within each (scope, polarity).
    """
    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for c in claims:
        k = key_fn(c)
        if k is not None:
            buckets[k].append(c)
    raw = {k: weighted_evidence(v, use_confidence=True, now=now) for k, v in buckets.items()}
    top: dict[tuple, float] = defaultdict(float)
    for (scope, pol, _rc), val in raw.items():
        top[(scope, pol)] = max(top[(scope, pol)], val)
    out = {}
    for k, val in raw.items():
        denom = top[(k[0], k[1])] or 1.0
        score = round(100 * val / denom, 1)
        out[k] = {
            "score": score,
            "weighted_evidence": val,
            "strength": strength_label(score),
            **_breakdown(buckets[k]),
        }
    return out


def segment_group_scores(claims: list[dict[str, Any]], now: datetime | None = None):
    return group_scores(
        claims,
        lambda c: (
            (c["segment"], c["polarity"], c["root_cause_ids"][0]) if c["root_cause_ids"] else None
        ),
        now,
    )


def industry_group_scores(
    claims: list[dict[str, Any]], industries: list[str], now: datetime | None = None
):
    out: dict[tuple, dict[str, Any]] = {}
    for ind in industries:
        subset = [c for c in claims if ind in c["industries"]]
        out.update(
            group_scores(
                subset,
                lambda c, ind=ind: (
                    (ind, c["polarity"], c["root_cause_ids"][0]) if c["root_cause_ids"] else None
                ),
                now,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Use-case scores
# ---------------------------------------------------------------------------
def use_case_scores(
    claims: list[dict[str, Any]], catalog: dict[str, Any] | None, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    w = weights()
    seg_of = {u["id"]: u["segment"] for u in (catalog or {}).get("use_cases", [])}
    per_uc: dict[str, dict[str, list]] = defaultdict(lambda: {"driver": [], "inhibitor": []})
    for c in claims:
        for uid in c.get("use_case_ids") or []:
            per_uc[uid][c["polarity"]].append(c)

    raw = {}
    for uid, pol in per_uc.items():
        d = weighted_evidence(pol["driver"], now=now)
        b = weighted_evidence(pol["inhibitor"], now=now)
        raw[uid] = (weighted_evidence(pol["driver"] + pol["inhibitor"], now=now), d, b)

    best_by_seg: dict[str, float] = defaultdict(float)
    for uid, (e, _, _) in raw.items():
        best_by_seg[seg_of.get(uid, "?")] = max(best_by_seg[seg_of.get(uid, "?")], e)

    out = {}
    for uid in set(seg_of) | set(raw):
        e, d, b = raw.get(uid, (0.0, 0.0, 0.0))
        best = best_by_seg.get(seg_of.get(uid, "?"), 0.0) or 1.0
        evidence = e / best
        positivity = d / (d + b) if (d + b) else 0.5
        score = (
            100
            * (evidence ** float(w["evidence_exponent"]))
            * (float(w["balance_floor"]) + (1 - float(w["balance_floor"])) * positivity)
        )
        claims_all = per_uc[uid]["driver"] + per_uc[uid]["inhibitor"] if uid in per_uc else []
        out[uid] = {
            "score": round(score, 1),
            "evidence": round(evidence, 3),
            "positivity": round(positivity, 3),
            "weighted_drivers": d,
            "weighted_blockers": b,
            **_breakdown(claims_all),
        }
    return out
