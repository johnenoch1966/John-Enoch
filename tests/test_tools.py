from __future__ import annotations

import json

from adoption_agent import config
from adoption_agent.schemas import RootCauseMappingInput
from adoption_agent.tools.evidence import (
    get_store,
    get_unmapped_claims,
    record_evidence,
    record_root_cause_mappings,
    validate_citations,
)
from adoption_agent.tools.scope import save_use_case_catalog, validate_catalog
from adoption_agent.tools.taxonomy import load_taxonomy, lookup_taxonomy, taxonomy_ids
from tests.samples import sample_catalog, sample_claim, sample_source


# ---------------------------------------------------------------- taxonomy
def test_taxonomy_ids_unique_and_examples_present():
    cats = load_taxonomy()
    assert len({c["id"] for c in cats}) == len(cats)
    assert all(c["signals"] and c["inference_examples"] for c in cats)


def test_connectors_maps_to_risk_and_auditability():
    """The canonical semantic-bias example from the problem statement."""
    res = lookup_taxonomy("Access to connectors not enabled")
    assert res["candidates"][0]["id"] == "risk_auditability"


def test_lookup_taxonomy_unknown_phrase_returns_all_ids():
    res = lookup_taxonomy("zzz qqq")
    assert res["candidates"] == []
    assert set(res["all_category_ids"]) == taxonomy_ids()


# ---------------------------------------------------------------- evidence
def test_record_evidence_persists_and_counts(tmp_home, tool_context):
    res = record_evidence(
        sample_source(1), [sample_claim(), sample_claim("ROI", "driver")], tool_context
    )
    assert res["status"] == "ok" and res["source_is_new"] and res["new_claims"] == 2
    again = record_evidence(sample_source(1), [sample_claim()], tool_context)
    assert not again["source_is_new"] and again["duplicate_claims"] == 1
    assert tool_context.state["temp:sources_by_test_agent"] == 1


def test_record_evidence_rejects_empty_claims(tmp_home, tool_context):
    assert record_evidence(sample_source(1), [], tool_context)["status"] == "error"


def test_mapping_validation_rejects_unknown_ids(tmp_home, tool_context):
    res = record_evidence(sample_source(1), [sample_claim()], tool_context)
    cid = res["claim_ids"][0]
    batch = get_unmapped_claims(tool_context)
    assert batch["remaining"] == 1

    out = record_root_cause_mappings(
        [
            RootCauseMappingInput(
                claim_id=cid, root_cause_ids=["not_a_category"], inference="x", industries=["fsi"]
            ),
            RootCauseMappingInput(
                claim_id=999,
                root_cause_ids=["risk_auditability"],
                inference="x",
                industries=["fsi"],
            ),
        ],
        tool_context,
    )
    assert out["saved"] == 0 and len(out["rejected"]) == 2 and out["remaining"] == 1

    ok = record_root_cause_mappings(
        [
            RootCauseMappingInput(
                claim_id=cid,
                root_cause_ids=["risk_auditability"],
                inference="x",
                industries=["fsi"],
            )
        ],
        tool_context,
    )
    assert ok["status"] == "ok" and ok["remaining"] == 0


def test_validate_citations(tmp_home, tool_context):
    cid = record_evidence(sample_source(1), [sample_claim()], tool_context)["claim_ids"][0]
    assert validate_citations([cid]) == {"valid": True, "missing": [], "checked": 1}
    assert validate_citations([cid, 42])["missing"] == [42]


# ------------------------------------------------------------------- scope
def test_catalog_validation_catches_structural_problems():
    cat = sample_catalog()
    assert validate_catalog(cat) == []
    cat.clusters[1].use_case_ids.append(cat.clusters[0].use_case_ids[0])  # wrong segment + dupe
    problems = " ".join(validate_catalog(cat))
    assert "segment does not match" in problems and "is in clusters" in problems


def test_save_catalog_writes_file_and_state(tmp_home, tool_context):
    cat = sample_catalog()
    cat.clusters.reverse()  # copilot cluster last; tool must reorder
    res = save_use_case_catalog(cat.use_cases, cat.clusters, tool_context)
    assert res["status"] == "ok" and res["use_cases"] == config.TARGET_USE_CASES
    saved = json.loads(config.USE_CASE_CATALOG.read_text())
    assert saved["clusters"][0]["segment"] == "copilot"
    assert tool_context.state["use_case_catalog"]["clusters"][0]["segment"] == "copilot"


def test_save_catalog_returns_problems_for_too_few(tmp_home, tool_context):
    cat = sample_catalog(10)
    res = save_use_case_catalog(cat.use_cases, cat.clusters, tool_context)
    assert res["status"] == "error" and any("Expected" in p for p in res["problems"])
    assert get_store().stats()["claims_total"] == 0


# -------------------------------------------------------------- industries
def test_industry_ids_in_sync_with_schema():
    from typing import get_args

    from adoption_agent.schemas import IndustryId, IndustryTag

    assert list(get_args(IndustryId)) == list(config.INDUSTRIES)
    assert set(get_args(IndustryTag)) == set(config.INDUSTRIES) | {"cross_industry"}


def test_canonical_industries_maps_free_text():
    from adoption_agent.industries import canonical_industries, use_case_industries

    assert canonical_industries(["Retail Banking", "Pharma", "Telecommunications"]) == [
        "fsi",
        "hcls",
        "tmt",
    ]
    assert use_case_industries({"industries": ["hcls"], "example_industries": ["Banking"]}) == [
        "hcls"
    ]
    assert use_case_industries({"example_industries": ["Automotive", "Utilities"]}) == [
        "manufacturing",
        "energy_utilities",
    ]
