"""Automated publication gate for a freshly generated report.

    python -m evals.gate_report            # checks data/report.json
    python -m evals.gate_report path.json

Applies the synthesis rubric (schema, coverage, completeness, citation
validity against the live evidence store, conciseness) with the
`synthesis_rubric` thresholds from evals/thresholds.json. Exit 0 = safe to
auto-publish; exit 1 = route to human review.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from adoption_agent import config
from adoption_agent.tools.evidence import get_store

from .metrics import industry_rubric, segment_rubric
from .run import ROOT


def gate(report_path: Path) -> tuple[bool, dict, list[str]]:
    report = json.loads(report_path.read_text())
    syn = report["synthesis"]
    valid = {c["id"] for c in get_store().all_claims()}
    catalog = report.get("catalog") or (
        json.loads(config.USE_CASE_CATALOG.read_text())
        if config.USE_CASE_CATALOG.exists()
        else None
    )
    scores = {
        **{
            f"segment.{k}": v
            for k, v in segment_rubric(
                {k: v for k, v in syn.items() if k != "industries"}, valid, catalog
            ).items()
        },
        **{f"industry.{k}": v for k, v in industry_rubric(syn, valid).items()},
    }
    rules = json.loads((ROOT / "thresholds.json").read_text())["synthesis_rubric"]
    failures = [f"{k}={scores.get(k)} < {v}" for k, v in rules.items() if scores.get(k, 0) < v]
    return not failures, scores, failures


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0]) if args else config.REPORT_JSON
    ok, scores, failures = gate(path)
    lines = ["## Publication quality gate", "", "| Metric | Score |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in scores.items()]
    lines += ["", "**PASSED: auto-publishing.**" if ok else "**FAILED: needs human review.**"]
    lines += [f"- {f}" for f in failures]
    md = "\n".join(lines)
    print(md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
