from __future__ import annotations

import json

from adoption_agent import config
from adoption_agent.report import build_report, diff_findings, publish, render_html
from adoption_agent.schemas import RootCauseMappingInput
from adoption_agent.store import EvidenceStore
from tests.samples import sample_catalog, sample_claim, sample_source, sample_synthesis


def _seed(store: EvidenceStore) -> list[int]:
    sid, _ = store.add_source(sample_source(1), "r1")
    ids = [store.add_claim(sid, sample_claim(f"Factor {i}"), "r1")[0] for i in range(3)]
    for i in ids:
        store.add_mapping(
            RootCauseMappingInput(
                claim_id=i,
                root_cause_ids=["risk_auditability"],
                inference="inf",
                industries=["fsi"],
            ),
            "r1",
        )
    return ids


def test_build_report_drops_invalid_citations(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    ids = _seed(store)
    report = build_report(
        sample_synthesis(ids + [999]),
        store,
        run_id="r1",
        catalog=sample_catalog().model_dump(),
        previous=None,
    )
    # 999 is cited by: 2 segments x (2 factors + 2 approaches + 2 use cases) = 12,
    # 1 bias insight, and 1 industry x (5 drivers + 5 blockers + 5 advice) = 15.
    assert report["stats"]["citations_dropped"] == 28
    assert [e["id"] for e in report["evidence"]] == ids
    risk = next(r for r in report["root_cause_matrix"] if r["id"] == "risk_auditability")
    assert risk["copilot_inhibitor"] == 3


def test_segment_views_rank_by_transparent_score(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    ids = _seed(store)  # 3 claims, 1 source, all about uc-0 (copilot, inhibitor)
    report = build_report(
        sample_synthesis(ids),
        store,
        run_id="r1",
        catalog=sample_catalog().model_dump(),
        previous=None,
    )
    copilot = report["segment_views"]["copilot"]
    ranked = copilot["ranked"]
    # Every catalog copilot use case is scored, not only those the AI described.
    assert len(ranked) == 10
    # uc-0 has evidence; uc-1 (AI rated 5/5) has none -> uc-0 first, uc-1 AI rating capped at 3.
    assert ranked[0]["use_case_id"] == "uc-0" and ranked[0]["score"] > 0
    uc1 = next(r for r in ranked if r["use_case_id"] == "uc-1")
    assert uc1["score"] == 0.0 and uc1["ai_rating"] == 3 and uc1["low_evidence"]
    assert ranked[0]["score_detail"]["sources"] == 1 and ranked[0]["evidence_count"] == 3
    assert "Use case 2" in copilot["not_assessed"]
    # AI-written blockers get the deterministic root-cause score and label.
    blocker = copilot["blockers"][0]
    assert blocker["score"] == 100.0 and blocker["strength"] == "strong"
    assert blocker["evidence_sources"] == 1


def test_industry_views_filter_unknown_ids_and_fall_back(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    ids = _seed(store)
    report = build_report(
        sample_synthesis(ids),
        store,
        run_id="r1",
        catalog=sample_catalog().model_dump(),
        previous=None,
    )
    views = {v["id"]: v for v in report["industry_views"]}
    assert list(views) == list(config.INDUSTRIES)
    fsi = views["fsi"]
    top_ids = [u["id"] for u in fsi["top_use_cases"]]
    assert top_ids[:2] == ["uc-10", "uc-0"] and "not-a-use-case" not in top_ids
    assert len(top_ids) >= 5  # topped up from catalog industry tags
    assert fsi["claim_count"] == 3 and fsi["profile"]["advice"][0]["action"] == "FSI action 1"
    assert views["hcls"]["profile"] is None and views["hcls"]["top_use_cases"]


def test_diff_detects_new_and_dropped_factors():
    prev_syn = sample_synthesis([1], driver="Board-level executive sponsorship")
    prev_syn.segments[0].top_drivers[0].root_cause_id = "cost_roi"
    prev = {"synthesis": json.loads(prev_syn.model_dump_json())}
    cur = sample_synthesis([1], driver="Unrelated new factor")
    d = diff_findings(cur, prev)
    drivers = d["segments"]["copilot"]["top_drivers"]
    assert drivers["new"] == ["Unrelated new factor"]
    assert drivers["dropped"] == ["Board-level executive sponsorship"]
    assert drivers["new_themes"] == ["Leadership, Ownership & Operating Model"]
    assert drivers["dropped_themes"] == ["Cost & ROI Clarity"]
    assert d["segments"]["copilot"]["top_inhibitors"]["persisting"] == ["Connectors disabled"]
    assert diff_findings(sample_synthesis([1]), None)["first_run"] is True


def test_diff_matches_reworded_factors():
    """LLMs re-word factors between runs; same meaning should persist."""
    prev = {
        "synthesis": json.loads(
            sample_synthesis([1], driver="Strong executive sponsorship").model_dump_json()
        )
    }
    d = diff_findings(sample_synthesis([1], driver="Executive sponsorship and ownership"), prev)
    drivers = d["segments"]["copilot"]["top_drivers"]
    assert drivers["persisting"] == ["Executive sponsorship and ownership"]
    assert drivers["new"] == [] and drivers["dropped"] == []


def test_render_and_publish_html(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    ids = _seed(store)
    report = build_report(
        sample_synthesis(ids),
        store,
        run_id="r1",
        catalog=sample_catalog().model_dump(),
        previous=None,
    )
    html = render_html(report)
    for needle in (
        "Top AI use cases by industry",
        "Copilot / desktop agentic assistants",
        "Workflow-specific agents",
        "Industry deep-dive",
        'data-ind="fsi"',
        "FSI action 1",
        "Semantic bias",
        "connectors not enabled",
        "Risk &amp; Auditability",
        'id="e1"',
        "How scores are calculated",
        "AI rating",
    ):
        assert needle in html, needle
    assert "Executive summary" not in html

    paths = publish(report)
    assert (config.REPORTS_DIR / "index.html").exists()
    assert (config.REPORTS_DIR / "history" / "r1.html").exists()
    assert json.loads(config.REPORT_JSON.read_text())["run_id"] == "r1"
    assert "history.jsonl" in str(list(config.DATA_DIR.iterdir()))
    assert paths["index"].endswith("index.html")


def test_html_escapes_untrusted_source_text(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    sid, _ = store.add_source(sample_source(9), "r1")
    cid, _ = store.add_claim(sid, sample_claim("<script>alert(1)</script>"), "r1")
    report = build_report(sample_synthesis([cid]), store, run_id="r1", catalog=None, previous=None)
    html = render_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_refresh_button_links_to_workflow(tmp_home, monkeypatch):
    store = EvidenceStore(config.EVIDENCE_DB)
    ids = _seed(store)
    report = build_report(sample_synthesis(ids), store, run_id="r1", catalog=None, previous=None)
    monkeypatch.setattr(config, "REPO_URL", "https://github.com/acme/repo")
    html = render_html(report)
    assert 'href="https://github.com/acme/repo/issues/new?template=refresh.yml"' in html
    assert "Refresh research" in html and "refresh-report.yml/badge.svg" in html
    monkeypatch.setattr(config, "REPO_URL", "")
    assert "Refresh research" not in render_html(report)
