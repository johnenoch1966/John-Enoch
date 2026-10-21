"""Turn a validated Synthesis + evidence store into an updatable HTML report.

Deterministic (no LLM): citations are re-validated against the store,
findings are diffed against the previous run, and every run is archived so
the report has a visible history.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import config
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


def segment_views(
    synthesis: Synthesis, catalog: dict[str, Any] | None, claims: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Use cases per segment ranked by impact, enriched with names and evidence."""
    use_cases = {u["id"]: u for u in (catalog or {}).get("use_cases", [])}
    rank_of = {uid: i + 1 for i, uid in enumerate(use_cases)}
    ev = EvidenceStore.use_case_evidence(claims)
    out: dict[str, dict[str, Any]] = {}
    for seg in synthesis.segments:
        rows, seen = [], set()
        for u in seg.use_case_impact:
            if u.use_case_id in seen:
                continue
            seen.add(u.use_case_id)
            meta = use_cases.get(u.use_case_id, {})
            e = ev.get(u.use_case_id, {"drivers": 0, "inhibitors": 0})
            n_ev = e["drivers"] + e["inhibitors"]
            row = {
                **u.model_dump(),
                "name": meta.get("name", u.use_case_id.replace("-", " ").title()),
                "prevalence_rank": rank_of.get(u.use_case_id),
                "evidence_count": n_ev,
                "low_evidence": n_ev == 0,
            }
            # Guard against unsupported optimism: without any direct evidence a
            # use case cannot be scored above the midpoint.
            if n_ev == 0 and row["impact_score"] > MAX_SCORE_WITHOUT_EVIDENCE:
                row["model_score"] = row["impact_score"]
                row["impact_score"] = MAX_SCORE_WITHOUT_EVIDENCE
            rows.append(row)
        rows.sort(key=lambda r: (-r["impact_score"], -r["evidence_count"]))
        not_assessed = [
            u["name"]
            for uid, u in use_cases.items()
            if u.get("segment") == seg.segment and uid not in seen
        ]
        out[seg.segment] = {"ranked": rows, "not_assessed": not_assessed}
    return out


def industry_views(
    synthesis: Synthesis, catalog: dict[str, Any] | None, claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One entry per canonical industry: top use cases + drivers/blockers/advice."""
    use_cases = (catalog or {}).get("use_cases", [])
    by_id = {u["id"]: u for u in use_cases}
    profiles = {p.industry_id: p for p in synthesis.industries}
    views = []
    for ind_id, ind_name in config.INDUSTRIES.items():
        p = profiles.get(ind_id)
        top_ids = [i for i in (p.top_use_case_ids if p else []) if i in by_id]
        if len(top_ids) < 5:  # fall back to catalog prevalence order
            top_ids += [
                u["id"]
                for u in use_cases
                if ind_id in use_case_industries(u) and u["id"] not in top_ids
            ]
        top = [
            {"id": i, "name": by_id[i]["name"], "segment": by_id[i]["segment"]}
            for i in dict.fromkeys(top_ids)
        ][:8]
        views.append(
            {
                "id": ind_id,
                "name": ind_name,
                "claim_count": sum(1 for c in claims if ind_id in c["industries"]),
                "top_use_cases": top,
                "profile": json.loads(p.model_dump_json()) if p else None,
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

    cited: set[int] = set()
    for item in _evidence_lists(synthesis):
        cited.update(item.evidence_ids)

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
        "segment_views": segment_views(synthesis, catalog, claims),
        "industry_views": industry_views(synthesis, catalog, claims),
        "diff": diff_findings(synthesis, previous),
        "root_cause_matrix": root_cause_matrix(claims),
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
    return env.get_template("report.html.j2").render(r=report, history=history or [])


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
