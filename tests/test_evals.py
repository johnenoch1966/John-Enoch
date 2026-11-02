"""Tests for the evaluation harness itself (metrics, gating logic, offline suite)."""

from __future__ import annotations

import json

from evals import run
from evals.metrics import (
    industry_rubric,
    pii_scores,
    root_cause_scores,
    segment_rubric,
    taxonomy_lookup_scores,
)
from tests.samples import sample_catalog, sample_synthesis

CASES = [
    {
        "id": "a",
        "stated_factor": "connectors not enabled",
        "expected_root_causes": ["risk_auditability"],
        "expected_industries": ["cross_industry"],
    },
    {
        "id": "b",
        "stated_factor": "licence cost",
        "expected_root_causes": ["cost_roi"],
        "expected_industries": ["fsi"],
    },
]


def test_root_cause_scores():
    preds = {
        "a": {"root_cause_ids": ["risk_auditability"], "industries": ["cross_industry"]},
        "b": {
            "root_cause_ids": ["operating_model", "cost_roi"],
            "industries": ["fsi", "cross_industry"],
        },
    }
    s = root_cause_scores(CASES, preds)
    assert s["top1_accuracy"] == 0.5 and s["any_accuracy"] == 1.0
    assert s["industry_match"] == 1.0 and s["coverage"] == 1.0


def test_root_cause_scores_penalise_missing_predictions():
    s = root_cause_scores(CASES, {})
    assert s == {
        "top1_accuracy": 0.0,
        "any_accuracy": 0.0,
        "industry_match": 0.0,
        "coverage": 0.0,
        "n": 2,
    }


def test_taxonomy_lookup_scores_hits_connectors_case():
    assert taxonomy_lookup_scores(CASES[:1])["top1_hit"] == 1.0


def test_pii_scores_detect_misses_and_false_positives():
    s = pii_scores(
        [
            {"text": "mail a@b.com", "expected_types": ["EMAIL"], "must_keep": ["mail"]},
            {"text": "62% in 2025", "expected_types": [], "must_keep": ["62%"]},
        ]
    )
    assert s == {"recall": 1.0, "false_positive_rate": 0.0, "keep_rate": 1.0, "n": 2}


def test_rubrics_reward_valid_and_penalise_invalid_citations():
    syn = json.loads(sample_synthesis([1, 2]).model_dump_json())
    good = segment_rubric(syn, {1, 2}, sample_catalog().model_dump())
    assert good["schema_valid"] == 1.0 and good["citation_validity"] == 1.0
    bad = segment_rubric(syn, {1}, sample_catalog().model_dump())
    assert bad["citation_validity"] < 1.0
    assert segment_rubric({}, set(), None)["schema_valid"] == 0.0
    # sample has 1 industry profile out of 7
    assert industry_rubric(syn, {1, 2})["industry_completeness"] < 0.2


def test_check_enforces_thresholds_and_baseline():
    results = {"s": {"acc": 0.70, "false_positive_rate": 0.2, "n": 5}}
    thresholds = {"s": {"acc": 0.75, "false_positive_rate": 0.05}}
    fails = run.check(results, thresholds, {"s": {"acc": 0.90}}, tolerance=0.05)
    assert len(fails) == 3  # below threshold, fp too high, regressed vs baseline
    assert run.check({"s": {"acc": 0.88}}, {}, {"s": {"acc": 0.90}}, 0.05) == []


def test_offline_suite_passes_its_gates(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RESULTS", tmp_path)
    assert run.main(["--suite", "offline", "--check"]) == 0
    out = json.loads((tmp_path / "latest.json").read_text())
    assert set(out["results"]) == {"pii_masking", "taxonomy_lookup", "synthesis_rubric"}


def test_golden_datasets_are_well_formed():
    from adoption_agent import config
    from adoption_agent.tools.taxonomy import taxonomy_ids

    rc = run._jsonl(run.GOLDEN / "root_cause.jsonl")
    assert len(rc) >= 30 and len({c["id"] for c in rc}) == len(rc)
    valid_inds = set(config.INDUSTRIES) | {"cross_industry"}
    for c in rc:
        assert set(c["expected_root_causes"]) <= taxonomy_ids(), c["id"]
        assert set(c["expected_industries"]) <= valid_inds, c["id"]
    assert any(c["stated_factor"] == "Access to connectors not enabled" for c in rc)
    pii = run._jsonl(run.GOLDEN / "pii.jsonl")
    assert sum(1 for c in pii if not c["expected_types"]) >= 10  # false-positive traps
