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
                claim_id=i, root_cause_ids=["risk_auditability"], inference="inf"
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
    # 999 appears in 2 segments x (2 factors + 2 approaches) + 1 bias insight = 9 times
    assert report["stats"]["citations_dropped"] == 9
    assert [e["id"] for e in report["evidence"]] == ids
    risk = next(r for r in report["root_cause_matrix"] if r["id"] == "risk_auditability")
    assert risk["copilot_inhibitor"] == 3


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
        "Executive summary",
        "Semantic bias",
        "connectors not enabled",
        "Risk &amp; Auditability",
        'id="e1"',
        "Use-case scope",
    ):
        assert needle in html, needle

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
