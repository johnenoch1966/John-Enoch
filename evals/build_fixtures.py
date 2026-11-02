"""Freeze evaluation fixtures from the current evidence base.

    python -m evals.build_fixtures

Fixtures are FROZEN inputs: regenerate them deliberately (and re-baseline)
when the evidence base changes materially; otherwise score changes would
reflect new data rather than agent regressions.
"""

from __future__ import annotations

import json

from adoption_agent import config
from adoption_agent.tools.evidence import get_store

from .run import FIXTURES


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    store = get_store()

    def dump(name: str, obj) -> None:
        (FIXTURES / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
        print(f"{name}: {(FIXTURES / name).stat().st_size:,} bytes")

    dump("evidence_digest.json", store.digest())
    dump("industry_digest.json", store.industry_digest(list(config.INDUSTRIES)))
    dump("catalog.json", json.loads(config.USE_CASE_CATALOG.read_text()))
    report = json.loads(config.REPORT_JSON.read_text())
    dump("synthesis_snapshot.json", report["synthesis"])
    dump("valid_claim_ids.json", sorted(c["id"] for c in store.all_claims()))


if __name__ == "__main__":
    main()
