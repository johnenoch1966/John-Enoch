from __future__ import annotations

import sqlite3

from adoption_agent import config
from adoption_agent.schemas import RootCauseMappingInput
from adoption_agent.store import EvidenceStore, normalise_url
from tests.samples import sample_claim, sample_source


def _map(store, cid, root="risk_auditability", industries=("cross_industry",)):
    store.add_mapping(
        RootCauseMappingInput(
            claim_id=cid,
            root_cause_ids=[root],
            inference="admins cannot audit",
            industries=list(industries),
        ),
        "r1",
    )


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

    _map(store, c1)
    assert store.count_unmapped() == 1
    assert [c["id"] for c in store.unmapped_claims()] == [c2]

    digest = store.digest()
    groups = {(g["segment"], g["polarity"], g["root_cause_id"]): g for g in digest["groups"]}
    risk = groups[("copilot", "inhibitor", "risk_auditability")]
    assert risk["claim_count"] == 1 and risk["vendor_share"] == 1.0
    assert risk["examples"][0]["inference"] == "admins cannot audit"
    assert ("workflow", "driver", "unmapped") in groups
    assert digest["use_case_evidence"]["uc-0"] == {
        "drivers": 1,
        "inhibitors": 1,
        "claim_ids": [c1, c2],
    }


def test_industries_union_and_industry_digest(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    sid, _ = store.add_source(sample_source(1), "r1")
    c1, _ = store.add_claim(sid, sample_claim("KYC backlog", industries=["fsi"]), "r1")
    c2, _ = store.add_claim(sid, sample_claim("Generic ROI", "driver"), "r1")
    _map(store, c1, industries=["fsi", "cross_industry"])
    _map(store, c2, root="cost_roi", industries=["cross_industry"])

    by_id = {c["id"]: c for c in store.all_claims()}
    assert by_id[c1]["industries"] == ["fsi"]  # cross_industry dropped when specific
    assert by_id[c2]["industries"] == ["cross_industry"]

    d = store.industry_digest(["fsi", "hcls"])
    assert d["fsi"]["claim_count"] == 1
    assert d["fsi"]["groups"][0]["root_cause_id"] == "risk_auditability"
    assert d["hcls"]["claim_count"] == 0
    assert d["cross_industry"]["claim_count"] == 1


def test_mappings_without_industries_are_remapped(tmp_home):
    """Mappings created before industry tagging are back-filled on the next run."""
    store = EvidenceStore(config.EVIDENCE_DB)
    sid, _ = store.add_source(sample_source(1), "r1")
    cid, _ = store.add_claim(sid, sample_claim(), "r1")
    _map(store, cid)
    assert store.count_unmapped() == 0
    with sqlite3.connect(config.EVIDENCE_DB) as c:
        c.execute("UPDATE mappings SET industries=NULL")
    assert store.count_unmapped() == 1
    assert store.stats()["mapped_total"] == 0


def test_migration_adds_industry_columns_to_legacy_db(tmp_home):
    with sqlite3.connect(config.EVIDENCE_DB) as c:
        c.executescript(
            "CREATE TABLE sources (id INTEGER PRIMARY KEY, url_hash TEXT UNIQUE, url TEXT,"
            " title TEXT, publisher TEXT, published TEXT, source_type TEXT,"
            " vendor_affiliated INTEGER, methodology TEXT, run_id TEXT, created_at TEXT);"
            "CREATE TABLE claims (id INTEGER PRIMARY KEY, claim_hash TEXT UNIQUE, source_id INT,"
            " stated_factor TEXT, polarity TEXT, segment TEXT, use_case_ids TEXT,"
            " evidence_quote TEXT, statistic TEXT, run_id TEXT, created_at TEXT);"
            "CREATE TABLE mappings (claim_id INTEGER PRIMARY KEY, root_cause_ids TEXT,"
            " inference TEXT, confidence TEXT, framing_bias TEXT, run_id TEXT, created_at TEXT);"
            "INSERT INTO sources VALUES (1,'h','https://x.org','t','p','2025','analyst',0,'',"
            "'r0','now');"
            "INSERT INTO claims VALUES (1,'c',1,'f','driver','copilot','[]','q','','r0','now');"
            "INSERT INTO mappings VALUES (1,'[\"cost_roi\"]','i','high','','r0','now');"
        )
    store = EvidenceStore(config.EVIDENCE_DB)
    claims = store.all_claims()
    assert len(claims) == 1 and claims[0]["industries"] == ["cross_industry"]
    assert store.count_unmapped() == 1  # legacy mapping needs industry back-fill
