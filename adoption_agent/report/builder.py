"""Turn a validated Synthesis + evidence store into an updatable HTML report.

Deterministic (no LLM): citations are re-validated against the store,
findings are diffed against the previous run, and every run is archived so
the report has a visible history.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import config, scoring
from ..industries import use_case_industries
from ..privacy import count_placeholders
from ..schemas import Synthesis
from ..store import EvidenceStore
from ..tools.taxonomy import taxonomy_names

_TEMPLATES = Path(__file__).resolve().parent / "templates"
MAX_SCORE_WITHOUT_EVIDENCE = 3


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


_STOP = {"and", "of", "the", "a", "an", "to", "in", "for", "with", "on", "by", "from", "or"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in _STOP}


def _same_factor(a: dict[str, Any], b: dict[str, Any], threshold: float = 0.3) -> bool:
    """LLMs re-word factors between runs, so match on meaning, not exact text.

    Two factors are the same if their wording overlaps strongly, or if they
    share a root cause and overlap at least moderately.
    """
    ta, tb = _tokens(a["factor"]), _tokens(b["factor"])
    jaccard = len(ta & tb) / max(len(ta | tb), 1)
    if jaccard >= 0.5:
        return True
    return a.get("root_cause_id") == b.get("root_cause_id") and jaccard >= threshold


def _evidence_lists(synthesis: Synthesis) -> list[Any]:
    """Every object in the synthesis that carries `evidence_ids`."""
    items: list[Any] = []
    for seg in synthesis.segments:
        items += seg.top_drivers + seg.top_inhibitors
        items += seg.high_adoption_approaches + seg.low_adoption_patterns
        items += seg.use_case_impact
    items += synthesis.semantic_bias_insights
    for ind in synthesis.industries:
        items += ind.drivers + ind.blockers + ind.advice
    return items


def sanitize_citations(synthesis: Synthesis, valid_ids: set[int]) -> tuple[Synthesis, int]:
    """Drop any evidence id that does not exist. Returns (synthesis, n_dropped)."""
    dropped = 0
    for item in _evidence_lists(synthesis):
        keep = [i for i in item.evidence_ids if i in valid_ids]
        dropped += len(item.evidence_ids) - len(keep)
        item.evidence_ids = keep
    return synthesis, dropped


def diff_findings(current: Synthesis, previous: dict[str, Any] | None) -> dict[str, Any]:
    """Which drivers/inhibitors and root-cause themes are new, persisting or dropped."""
    names = taxonomy_names()
    out: dict[str, Any] = {"first_run": previous is None, "segments": {}}
    prev_segments = {
        s["segment"]: s for s in ((previous or {}).get("synthesis", {}).get("segments", []))
    }
    for seg in current.segments:
        p = prev_segments.get(seg.segment, {})
        seg_diff = {}
        for key in ("top_drivers", "top_inhibitors"):
            now = [f.model_dump() for f in getattr(seg, key)]
            before = p.get(key, [])
            matched_before: set[int] = set()
            new, persisting = [], []
            for f in now:
                hit = next(
                    (
                        i
                        for i, b in enumerate(before)
                        if i not in matched_before and _same_factor(f, b)
                    ),
                    None,
                )
                if hit is None:
                    new.append(f["factor"])
                else:
                    matched_before.add(hit)
                    persisting.append(f["factor"])
            now_rc = {f["root_cause_id"] for f in now}
            before_rc = {b.get("root_cause_id") for b in before}
            seg_diff[key] = {
                "new": new if previous else [],
                "persisting": persisting,
                "dropped": [b["factor"] for i, b in enumerate(before) if i not in matched_before],
                "new_themes": sorted(names.get(r, r) for r in now_rc - before_rc)
                if previous
                else [],
                "dropped_themes": sorted(names.get(r, r) for r in before_rc - now_rc if r),
            }
        out["segments"][seg.segment] = seg_diff
    return out


def root_cause_matrix(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Counts of mapped claims by root cause x segment x polarity (for heatmap)."""
    names = taxonomy_names()
    counts = Counter(
        (c["root_cause_ids"][0], c["segment"], c["polarity"]) for c in claims if c["root_cause_ids"]
    )
    rows = []
    for rid, name in names.items():
        row = {"id": rid, "name": name}
        for seg in ("copilot", "workflow"):
            for pol in ("driver", "inhibitor"):
                row[f"{seg}_{pol}"] = counts.get((rid, seg, pol), 0)
        row["total"] = sum(v for k, v in row.items() if k.endswith(("driver", "inhibitor")))
        rows.append(row)
    rows.sort(key=lambda r: -r["total"])
    return rows


def _score_factors(
    items: list[dict],
    scores: dict,
    scope: str,
    polarity: str,
    claims_by_id: dict[int, dict[str, Any]],
) -> list[dict]:
    """Attach the deterministic root-cause score to each AI-written factor and
    rank by it (ties broken by the AI's original order).

    Scores are computed per root-cause theme. When several factors in the same
    list share a theme, the theme score is split between them in proportion to
    the weighted evidence (of the same polarity) each one cites, or equally if
    none, so distinct factors never show identical, double-counted scores.
    """
    siblings: dict[Any, list[int]] = defaultdict(list)
    for idx, item in enumerate(items):
        siblings[item.get("root_cause_id")].append(idx)

    own = []
    for item in items:
        cited = [
            claims_by_id[i]
            for i in item.get("evidence_ids", [])
            if i in claims_by_id and claims_by_id[i]["polarity"] == polarity
        ]
        own.append(scoring.weighted_evidence(cited, use_confidence=True))

    out = []
    for order, item in enumerate(items):
        sc = scores.get((scope, polarity, item.get("root_cause_id")))
        enriched = {**item, "ai_order": order + 1}
        if sc:
            group = siblings[item.get("root_cause_id")]
            share = 1.0
            if len(group) > 1:
                total = sum(own[i] for i in group)
                share = own[order] / total if total else 1 / len(group)
            score = round(sc["score"] * share, 1)
            enriched.update(
                score=score,
                strength=scoring.strength_label(score),
                theme_score=sc["score"],
                theme_share=round(share, 3),
                shared_theme=len(group) > 1,
                evidence_sources=sc["sources"],
                evidence_claims=sc["claims"],
                vendor_share=sc["vendor_share"],
            )
        else:
            enriched.update(
                score=0.0,
                strength="weak",
                shared_theme=False,
                evidence_sources=0,
                evidence_claims=0,
            )
        out.append(enriched)
    out.sort(key=lambda f: (-f["score"], f["ai_order"]))
    return out


def _use_case_citations(
    ai_ids: list[int], tagged: list[dict[str, Any]], limit: int = 4
) -> list[int]:
    """Citations shown next to a use case, restricted to claims tagged with it.

    Keeps the analyst's choice where it is valid; otherwise falls back to the
    strongest tagged claims, so the links always match the claim count shown.
    """
    tagged_ids = {c["id"] for c in tagged}
    keep = [i for i in ai_ids if i in tagged_ids]
    if keep:
        return keep
    best = sorted(tagged, key=lambda c: (-scoring.claim_weight(c), c["id"]))
    return [c["id"] for c in best[:limit]]


def segment_views(
    synthesis: Synthesis, catalog: dict[str, Any] | None, claims: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Use cases per segment ranked by the transparent evidence score.

    Every catalog use case is scored (even ones the analyst skipped); the AI
    contributes labels (stage, value lever, blocker) and an advisory rating.
    """
    use_cases = {u["id"]: u for u in (catalog or {}).get("use_cases", [])}
    rank_of = {uid: i + 1 for i, uid in enumerate(use_cases)}
    uc_scores = scoring.use_case_scores(claims, catalog)
    grp = scoring.segment_group_scores(claims)
    claims_by_id = {c["id"]: c for c in claims}
    tagged: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in claims:
        for uid in c.get("use_case_ids") or []:
            tagged[uid].append(c)
    out: dict[str, dict[str, Any]] = {}
    for seg in synthesis.segments:
        ai = {u.use_case_id: u.model_dump() for u in seg.use_case_impact}
        ids = [uid for uid, u in use_cases.items() if u.get("segment") == seg.segment] or list(ai)
        rows = []
        for uid in dict.fromkeys(ids):
            sc = uc_scores.get(uid, {"score": 0.0, "claims": 0, "sources": 0})
            a = ai.get(uid, {})
            ai_rating = a.get("impact_score")
            if ai_rating and sc.get("claims", 0) == 0:
                ai_rating = min(ai_rating, MAX_SCORE_WITHOUT_EVIDENCE)
            rows.append(
                {
                    "use_case_id": uid,
                    "name": use_cases.get(uid, {}).get("name", uid.replace("-", " ").title()),
                    "prevalence_rank": rank_of.get(uid),
                    "score": sc["score"],
                    "score_detail": sc,
                    "ai_rating": ai_rating,
                    "adoption_stage": a.get("adoption_stage"),
                    "value_lever": a.get("value_lever", ""),
                    "key_blocker": a.get("key_blocker", ""),
                    "evidence_ids": _use_case_citations(a.get("evidence_ids", []), tagged[uid]),
                    "evidence_count": sc.get("claims", 0),
                    "low_evidence": sc.get("claims", 0) == 0,
                    "assessed": uid in ai,
                }
            )
        rows.sort(key=lambda r: (-r["score"], -(r["ai_rating"] or 0)))
        seg_d = seg.model_dump()
        out[seg.segment] = {
            "ranked": rows,
            "not_assessed": [r["name"] for r in rows if not r["assessed"]],
            "drivers": _score_factors(
                seg_d["top_drivers"], grp, seg.segment, "driver", claims_by_id
            ),
            "blockers": _score_factors(
                seg_d["top_inhibitors"], grp, seg.segment, "inhibitor", claims_by_id
            ),
        }
    return out


def industry_views(
    synthesis: Synthesis, catalog: dict[str, Any] | None, claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One entry per canonical industry: top use cases + scored drivers/blockers/advice.

    Top use cases are ranked by industry-specific weighted evidence (ties keep
    the analyst's order, then catalog order), consistent with the rest of the page.
    """
    use_cases = (catalog or {}).get("use_cases", [])
    by_id = {u["id"]: u for u in use_cases}
    profiles = {p.industry_id: p for p in synthesis.industries}
    grp = scoring.industry_group_scores(claims, list(config.INDUSTRIES))
    claims_by_id = {c["id"]: c for c in claims}
    views = []
    for ind_id, ind_name in config.INDUSTRIES.items():
        p = profiles.get(ind_id)
        ind_claims = [c for c in claims if ind_id in c["industries"]]
        per_uc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in ind_claims:
            for uid in c.get("use_case_ids") or []:
                if uid in by_id:
                    per_uc[uid].append(c)
        ai_ids = [i for i in (p.top_use_case_ids if p else []) if i in by_id]
        candidates = list(
            dict.fromkeys(
                ai_ids
                + [u["id"] for u in use_cases if ind_id in use_case_industries(u)]
                + list(per_uc)
            )
        )
        ai_pos = {uid: i for i, uid in enumerate(ai_ids)}
        cat_pos = {u["id"]: i for i, u in enumerate(use_cases)}
        weight = {uid: scoring.weighted_evidence(per_uc.get(uid, [])) for uid in candidates}
        candidates.sort(
            key=lambda uid: (-weight[uid], ai_pos.get(uid, len(ai_pos)), cat_pos.get(uid, 0))
        )
        top = [
            {
                "id": i,
                "name": by_id[i]["name"],
                "segment": by_id[i]["segment"],
                "claims": len(per_uc.get(i, [])),
            }
            for i in candidates[:8]
        ]
        profile = json.loads(p.model_dump_json()) if p else None
        if profile:
            profile["drivers"] = _score_factors(
                profile["drivers"], grp, ind_id, "driver", claims_by_id
            )
            profile["blockers"] = _score_factors(
                profile["blockers"], grp, ind_id, "inhibitor", claims_by_id
            )
        views.append(
            {
                "id": ind_id,
                "name": ind_name,
                "claim_count": len(ind_claims),
                "top_use_cases": top,
                "profile": profile,
            }
        )
    return views


def build_report(
    synthesis: Synthesis,
    store: EvidenceStore,
    *,
    run_id: str,
    catalog: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    run_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    claims = store.all_claims()
    by_id = {c["id"]: c for c in claims}
    synthesis, dropped = sanitize_citations(synthesis, set(by_id))
    seg_views = segment_views(synthesis, catalog, claims)

    cited: set[int] = set()
    for item in _evidence_lists(synthesis):
        cited.update(item.evidence_ids)
    # Use-case rows may cite fallback claims; keep their appendix anchors valid.
    for view in seg_views.values():
        for row in view["ranked"]:
            cited.update(row["evidence_ids"])

    sources = {c["source_id"] for c in claims}
    pii = Counter()
    for c in claims:
        for field in ("stated_factor", "evidence_quote", "statistic", "title", "url"):
            pii.update(count_placeholders(c.get(field) or ""))
    return {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": config.REASONING_MODEL,
        "synthesis": json.loads(synthesis.model_dump_json()),
        "segment_views": seg_views,
        "industry_views": industry_views(synthesis, catalog, claims),
        "diff": diff_findings(synthesis, previous),
        "root_cause_matrix": root_cause_matrix(claims),
        "scoring": scoring.weights(),
        "taxonomy": taxonomy_names(),
        "catalog": catalog,
        "evidence": [by_id[i] for i in sorted(cited)],
        "stats": {
            **store.stats(run_id),
            "sources_cited": len({by_id[i]["source_id"] for i in cited}),
            "claims_cited": len(cited),
            "citations_dropped": dropped,
            "pii_masked_total": int(sum(pii.values())),
            "pii_masked_by_type": dict(pii),
            "sources_total": len(sources),
            "vendor_source_share": round(
                sum(1 for c in claims if c["vendor_affiliated"]) / len(claims), 2
            )
            if claims
            else 0.0,
        },
        "run_stats": run_stats or {},
    }


def load_previous() -> dict[str, Any] | None:
    if config.REPORT_JSON.exists():
        return json.loads(config.REPORT_JSON.read_text(encoding="utf-8"))
    return None


def load_history() -> list[dict[str, Any]]:
    p = config.DATA_DIR / "history.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def render_html(report: dict[str, Any], history: list[dict[str, Any]] | None = None) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES), autoescape=select_autoescape(["html", "j2"])
    )
    return env.get_template("report.html.j2").render(
        r=report, history=history or [], repo_url=config.REPO_URL
    )


def publish(report: dict[str, Any]) -> dict[str, str]:
    """Write data/report.json, reports/index.html and an archived copy."""
    config.ensure_dirs()
    hist_entry = {
        "run_id": report["run_id"],
        "generated_at": report["generated_at"],
        "sources_total": report["stats"].get("sources_total", 0),
        "sources_new": report["stats"].get("sources_new", 0),
        "claims_total": report["stats"].get("claims_total", 0),
        "claims_new": report["stats"].get("claims_new", 0),
    }
    with open(config.DATA_DIR / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(hist_entry) + "\n")
    html = render_html(report, load_history())
    config.REPORT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    # Keep every run's structured report so diffs can always be recomputed.
    hist_json = config.DATA_DIR / "history" / f"{report['run_id']}.json"
    hist_json.parent.mkdir(parents=True, exist_ok=True)
    hist_json.write_text(
        json.dumps({k: report[k] for k in ("run_id", "generated_at", "synthesis", "stats")}),
        encoding="utf-8",
    )
    index = config.REPORTS_DIR / "index.html"
    archived = config.REPORTS_DIR / "history" / f"{report['run_id']}.html"
    index.write_text(html, encoding="utf-8")
    archived.write_text(html, encoding="utf-8")
    return {"index": str(index), "archived": str(archived), "json": str(config.REPORT_JSON)}


# ---------------------------------------------------------------------------
# Human-in-the-loop drafts
# ---------------------------------------------------------------------------
def _draft_paths() -> tuple[Path, Path]:
    return config.DATA_DIR / "draft_report.json", config.REPORTS_DIR / "draft.html"


def save_draft(report: dict[str, Any]) -> dict[str, str]:
    """Save an unapproved report for review without touching the public one."""
    config.ensure_dirs()
    js, html = _draft_paths()
    js.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    html.write_text(render_html(report, load_history()), encoding="utf-8")
    return {"json": str(js), "html": str(html)}


def publish_draft(approver: str = "human (publish-draft)") -> dict[str, str]:
    """Promote a reviewed draft to the public report (explicit human action)."""
    js, html = _draft_paths()
    if not js.exists():
        raise FileNotFoundError("No draft report to publish.")
    report = json.loads(js.read_text(encoding="utf-8"))
    report["approval"] = {**report.get("approval", {}), "approved": True, "decided_by": approver}
    paths = publish(report)
    js.unlink()
    html.unlink(missing_ok=True)
    return paths
