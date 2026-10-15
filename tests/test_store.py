from __future__ import annotations

from adoption_agent import config
from adoption_agent.schemas import RootCauseMappingInput
from adoption_agent.store import EvidenceStore, normalise_url
from tests.samples import sample_claim, sample_source


def test_normalise_url_strips_tracking_and_fragments():
    assert (
        normalise_url("HTTPS://Example.org/a/?utm_source=x&id=3#top")
        == "https://example.org/a?id=3"
    )


def test_sources_and_claims_are_deduplicated(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    sid, new = store.add_source(sample_source(1), "r1")
    sid2, new2 = store.add_source(sample_source(1), "r2")
    assert new and not new2 and sid == sid2

    cid, cnew = store.add_claim(sid, sample_claim(), "r1")
    cid2, cnew2 = store.add_claim(sid, sample_claim(), "r2")
    assert cnew and not cnew2 and cid == cid2
    assert store.stats("r1") == {
        "sources_total": 1,
        "claims_total": 1,
        "mapped_total": 0,
        "sources_new": 1,
        "claims_new": 1,
    }


def test_unmapped_and_digest(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    sid, _ = store.add_source(sample_source(1, vendor=True), "r1")
    c1, _ = store.add_claim(sid, sample_claim("Connectors not enabled"), "r1")
    c2, _ = store.add_claim(sid, sample_claim("Clear ROI", "driver", "workflow"), "r1")
    assert store.count_unmapped() == 2

    store.add_mapping(
        RootCauseMappingInput(
            claim_id=c1, root_cause_ids=["risk_auditability"], inference="admins cannot audit"
        ),
        "r1",
    )
    assert store.count_unmapped() == 1
    assert [c["id"] for c in store.unmapped_claims()] == [c2]

    digest = store.digest()
    groups = {(g["segment"], g["polarity"], g["root_cause_id"]): g for g in digest["groups"]}
    risk = groups[("copilot", "inhibitor", "risk_auditability")]
    assert risk["claim_count"] == 1 and risk["vendor_share"] == 1.0
    assert risk["examples"][0]["inference"] == "admins cannot audit"
    assert ("workflow", "driver", "unmapped") in groups
