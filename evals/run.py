"""Evaluation runner: golden datasets, thresholds and regression baseline.

    python -m evals.run --suite offline --check     # deterministic, every CI run
    python -m evals.run --suite live --check        # real model calls (needs ADC)
    python -m evals.run --suite all --update-baseline

Suites
  offline
    pii_masking        golden PII set incl. false-positive traps
    taxonomy_lookup    root-cause hint tool vs golden labels
    synthesis_rubric   rubric on the last published brief (fixture snapshot)
  live (generative agents, exact production configuration)
    root_cause_agent   semantic-bias agent maps 32 golden claims via its real tools
    segment_analyst    ranks use cases / drivers on a frozen evidence digest
    industry_analyst   builds 7 industry profiles on a frozen industry digest

`--check` fails (exit 1) if any metric is below its threshold in
evals/thresholds.json, or regresses more than the tolerance versus
evals/baseline.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
GOLDEN = ROOT / "golden"
FIXTURES = ROOT / "fixtures"
RESULTS = ROOT / "results"


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# --------------------------------------------------------------------------
# Offline suites
# --------------------------------------------------------------------------
def suite_pii() -> dict:
    from .metrics import pii_scores

    return pii_scores(_jsonl(GOLDEN / "pii.jsonl"))


def suite_taxonomy() -> dict:
    from .metrics import taxonomy_lookup_scores

    return taxonomy_lookup_scores(_jsonl(GOLDEN / "root_cause.jsonl"))


def suite_synthesis_rubric() -> dict:
    from .metrics import industry_rubric, segment_rubric

    snap = _fixture("synthesis_snapshot.json")
    valid = set(_fixture("valid_claim_ids.json"))
    catalog = _fixture("catalog.json")
    seg = segment_rubric({k: v for k, v in snap.items() if k != "industries"}, valid, catalog)
    ind = industry_rubric(snap, valid)
    return {
        **{f"segment.{k}": v for k, v in seg.items()},
        **{f"industry.{k}": v for k, v in ind.items()},
    }


# --------------------------------------------------------------------------
# Live suites (real model calls through ADK, isolated temp storage)
# --------------------------------------------------------------------------
class _TempHome:
    """Point all agent storage at a throwaway directory for the eval."""

    def __enter__(self):
        from adoption_agent import config

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._saved = {
            k: getattr(config, k)
            for k in (
                "PROJECT_ROOT",
                "DATA_DIR",
                "REPORTS_DIR",
                "RUNS_DIR",
                "EVIDENCE_DB",
                "USE_CASE_CATALOG",
                "REPORT_JSON",
            )
        }
        config.PROJECT_ROOT = base
        config.DATA_DIR = base / "data"
        config.REPORTS_DIR = base / "reports"
        config.RUNS_DIR = base / "runs"
        config.EVIDENCE_DB = base / "data" / "evidence.db"
        config.USE_CASE_CATALOG = base / "data" / "use_cases.json"
        config.REPORT_JSON = base / "data" / "report.json"
        config.ensure_dirs()
        return base

    def __exit__(self, *exc):
        from adoption_agent import config

        for k, v in self._saved.items():
            setattr(config, k, v)
        self._tmp.cleanup()


async def _run_agent(agent, state: dict) -> dict:
    from google.adk.agents import RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    runner = Runner(agent=agent, app_name="evals", session_service=InMemorySessionService())
    session = await runner.session_service.create_session(
        app_name="evals", user_id="eval", state=state
    )
    async for _ in runner.run_async(
        user_id="eval",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="Run.")]),
        run_config=RunConfig(max_llm_calls=200),
    ):
        pass
    final = await runner.session_service.get_session(
        app_name="evals", user_id="eval", session_id=session.id
    )
    await runner.close()
    return dict(final.state)


def suite_root_cause_agent() -> dict:
    from adoption_agent.agent import make_root_cause_mapping_loop
    from adoption_agent.schemas import ClaimInput, SourceInput
    from adoption_agent.tools.evidence import get_store

    from .metrics import root_cause_scores

    cases = _jsonl(GOLDEN / "root_cause.jsonl")
    with _TempHome():
        store = get_store()
        id_of: dict[int, str] = {}
        for c in cases:
            sid, _ = store.add_source(
                SourceInput(
                    url=f"https://golden.example/{c['id']}",
                    title=c["stated_factor"],
                    publisher=c["publisher"],
                    published="2025",
                    source_type=c["source_type"],
                    vendor_affiliated=c["vendor_affiliated"],
                ),
                "eval",
            )
            cid, _ = store.add_claim(
                sid,
                ClaimInput(
                    stated_factor=c["stated_factor"],
                    polarity=c["polarity"],
                    segment=c["segment"],
                    evidence_quote=c["evidence_quote"],
                ),
                "eval",
            )
            id_of[cid] = c["id"]
        asyncio.run(_run_agent(make_root_cause_mapping_loop(), {"run_id": "eval"}))
        preds = {
            id_of[cl["id"]]: {
                "root_cause_ids": cl["root_cause_ids"],
                "industries": cl["industries"],
            }
            for cl in store.all_claims()
            if cl["id"] in id_of and cl["root_cause_ids"]
        }
    return root_cause_scores(cases, preds)


def _analyst_state() -> dict:
    return {
        "evidence_digest": json.dumps(_fixture("evidence_digest.json")),
        "industry_digest": json.dumps(_fixture("industry_digest.json")),
        "use_case_catalog": _fixture("catalog.json"),
        "previous_summary": "",
        "evidence_totals": {"claims_total": 1},
    }


def _as_dict(value: Any) -> dict:
    return json.loads(value) if isinstance(value, str) else (value or {})


def suite_segment_analyst() -> dict:
    from adoption_agent.agent import make_segment_analyst

    from .metrics import segment_rubric

    with _TempHome():
        state = asyncio.run(_run_agent(make_segment_analyst(), _analyst_state()))
    out = _as_dict(state.get("synthesis_segments"))
    return segment_rubric(out, set(_fixture("valid_claim_ids.json")), _fixture("catalog.json"))


def suite_industry_analyst() -> dict:
    from adoption_agent.agent import make_industry_analyst

    from .metrics import industry_rubric

    with _TempHome():
        state = asyncio.run(_run_agent(make_industry_analyst(), _analyst_state()))
    out = _as_dict(state.get("synthesis_industries"))
    return industry_rubric(out, set(_fixture("valid_claim_ids.json")))


SUITES = {
    "offline": {
        "pii_masking": suite_pii,
        "taxonomy_lookup": suite_taxonomy,
        "synthesis_rubric": suite_synthesis_rubric,
    },
    "live": {
        "root_cause_agent": suite_root_cause_agent,
        "segment_analyst": suite_segment_analyst,
        "industry_analyst": suite_industry_analyst,
    },
}


# --------------------------------------------------------------------------
# Thresholds, baseline, reporting
# --------------------------------------------------------------------------
def check(results: dict, thresholds: dict, baseline: dict, tolerance: float) -> list[str]:
    """Return a list of failures (empty = pass)."""
    failures = []
    for suite, metrics in results.items():
        for name, value in metrics.items():
            if name == "n" or not isinstance(value, (int, float)):
                continue
            rule = thresholds.get(suite, {}).get(name)
            if rule is not None:
                lower_is_better = name.endswith("_rate") and "false_positive" in name
                ok = value <= rule if lower_is_better else value >= rule
                if not ok:
                    failures.append(f"{suite}.{name}={value} violates threshold {rule}")
            base = baseline.get(suite, {}).get(name)
            if base is not None and "false_positive" not in name and value < base - tolerance:
                failures.append(
                    f"{suite}.{name}={value} regressed from baseline {base} (tolerance {tolerance})"
                )
    return failures


def to_markdown(results: dict, failures: list[str], seconds: float) -> str:
    lines = ["## Agent evaluation results", "", "| Suite | Metric | Score |", "|---|---|---|"]
    for suite, metrics in results.items():
        for name, value in metrics.items():
            lines.append(f"| {suite} | {name} | {value} |")
    lines += ["", f"Runtime: {seconds:.0f}s", ""]
    lines += (
        ["**FAILED**"] + [f"- {f}" for f in failures] if failures else ["**All checks passed.**"]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.run")
    ap.add_argument("--suite", choices=["offline", "live", "all"], default="offline")
    ap.add_argument("--only", nargs="*", help="run only these sub-suites")
    ap.add_argument("--check", action="store_true", help="enforce thresholds + baseline")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.05)
    args = ap.parse_args(argv)

    groups = ["offline", "live"] if args.suite == "all" else [args.suite]
    t0 = time.time()
    results: dict[str, dict] = {}
    for g in groups:
        for name, fn in SUITES[g].items():
            if args.only and name not in args.only:
                continue
            print(f"[evals] running {name} ...", flush=True)
            results[name] = fn()
    thresholds = json.loads((ROOT / "thresholds.json").read_text())
    baseline_path = ROOT / "baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    failures = check(results, thresholds, baseline, args.tolerance) if args.check else []

    RESULTS.mkdir(exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }
    (RESULTS / "latest.json").write_text(json.dumps(payload, indent=2))
    md = to_markdown(results, failures, time.time() - t0)
    print(md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    if args.update_baseline:
        baseline.update(results)
        baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        print(f"[evals] baseline updated: {baseline_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
