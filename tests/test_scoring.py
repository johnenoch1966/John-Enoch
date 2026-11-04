"""Transparent scoring model: weights, recency, de-duplication, ranking."""

from __future__ import annotations

from datetime import UTC, datetime

from adoption_agent import scoring

NOW = datetime(2026, 11, 4, tzinfo=UTC)


def _c(
    cid,
    source_id,
    stype="analyst",
    vendor=False,
    published="2026-06",
    polarity="driver",
    segment="workflow",
    root="cost_roi",
    ucs=("uc-1",),
    conf="high",
    industries=("fsi",),
):
    return {
        "id": cid,
        "source_id": source_id,
        "source_type": stype,
        "vendor_affiliated": vendor,
        "published": published,
        "polarity": polarity,
        "segment": segment,
        "root_cause_ids": [root],
        "use_case_ids": list(ucs),
        "confidence": conf,
        "industries": list(industries),
    }


def test_source_weights_and_vendor_cap():
    assert scoring.source_weight("regulator", False) == 1.0
    assert scoring.source_weight("vendor", False) == 0.5
    assert scoring.source_weight("analyst", True) == 0.5  # vendor-sponsored analyst report capped
    assert scoring.source_weight("unknown-type", False) == 0.5


def test_recency_bands():
    assert scoring.recency_weight("2026-06", NOW) == 1.0
    assert scoring.recency_weight("2025-03", NOW) == 0.8
    assert scoring.recency_weight("2024", NOW) == 0.6
    assert scoring.recency_weight("2019-01", NOW) == 0.4
    assert scoring.recency_weight("unknown", NOW) == 0.7


def test_one_source_counts_once():
    same_source = [_c(1, "s1"), _c(2, "s1"), _c(3, "s1")]
    two_sources = [_c(1, "s1"), _c(2, "s2")]
    assert scoring.weighted_evidence(same_source, now=NOW) == 0.9
    assert scoring.weighted_evidence(two_sources, now=NOW) == 1.8


def test_group_scores_normalise_within_scope_and_polarity():
    claims = [
        _c(1, "s1", root="cost_roi"),
        _c(2, "s2", root="cost_roi"),
        _c(3, "s3", root="operating_model"),
        _c(4, "s4", polarity="inhibitor", root="risk_auditability", stype="vendor"),
    ]
    g = scoring.segment_group_scores(claims, now=NOW)
    assert g[("workflow", "driver", "cost_roi")]["score"] == 100.0
    assert g[("workflow", "driver", "operating_model")]["score"] == 50.0
    assert g[("workflow", "driver", "operating_model")]["strength"] == "moderate"
    # the only blocker group is 100 within its own polarity, even with a weak source
    assert g[("workflow", "inhibitor", "risk_auditability")]["score"] == 100.0
    assert g[("workflow", "driver", "cost_roi")]["sources"] == 2


def test_industry_scores_only_use_that_industry():
    claims = [
        _c(1, "s1", industries=("fsi",)),
        _c(2, "s2", industries=("hcls",), root="data_governance"),
    ]
    g = scoring.industry_group_scores(claims, ["fsi", "hcls"], now=NOW)
    assert ("fsi", "driver", "cost_roi") in g and ("fsi", "driver", "data_governance") not in g


def test_use_case_score_rewards_evidence_and_positive_balance():
    catalog = {"use_cases": [{"id": f"uc-{i}", "segment": "workflow"} for i in (1, 2, 3, 4)]}
    claims = (
        [_c(i, f"a{i}", ucs=("uc-1",)) for i in range(4)]  # 4 sources, all drivers
        + [
            _c(10 + i, f"b{i}", ucs=("uc-2",), polarity="inhibitor") for i in range(4)
        ]  # 4 sources, all blockers
        + [_c(20, "c0", ucs=("uc-3",))]  # 1 source, driver
    )
    s = scoring.use_case_scores(claims, catalog, now=NOW)
    assert s["uc-1"]["score"] == 100.0
    assert s["uc-1"]["score"] > s["uc-3"]["score"] > 0  # more evidence ranks higher
    assert s["uc-1"]["score"] > s["uc-2"]["score"]  # positive balance ranks higher
    assert s["uc-2"]["score"] == 40.0  # all-blocker floor
    assert s["uc-4"]["score"] == 0.0 and s["uc-4"]["claims"] == 0  # no evidence


def test_weights_file_is_complete():
    w = scoring.weights()
    for key in (
        "source_weights",
        "vendor_affiliated_cap",
        "recency",
        "confidence_weights",
        "evidence_exponent",
        "balance_floor",
        "strength_bands",
    ):
        assert key in w
