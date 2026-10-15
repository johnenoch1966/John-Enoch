"""Scope tool: validate and persist the top-N agent use-case catalog."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from google.adk.tools import ToolContext

from .. import config
from ..schemas import UseCase, UseCaseCatalog, UseCaseCluster


def validate_catalog(catalog: UseCaseCatalog) -> list[str]:
    """Returns a list of human-readable problems (empty if valid)."""
    problems: list[str] = []
    n = len(catalog.use_cases)
    lo, hi = int(config.TARGET_USE_CASES * 0.9), int(config.TARGET_USE_CASES * 1.1)
    if not lo <= n <= hi:
        problems.append(f"Expected ~{config.TARGET_USE_CASES} use cases, got {n}.")
    ids = [u.id for u in catalog.use_cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        problems.append(f"Duplicate use case ids: {sorted(dupes)}.")
    copilot = [c for c in catalog.clusters if c.segment == "copilot"]
    workflow = [c for c in catalog.clusters if c.segment == "workflow"]
    if len(copilot) != 1:
        problems.append(f"Need exactly 1 copilot cluster, got {len(copilot)}.")
    if len(workflow) != config.NUM_WORKFLOW_CLUSTERS:
        problems.append(
            f"Need exactly {config.NUM_WORKFLOW_CLUSTERS} workflow clusters, got {len(workflow)}."
        )
    seen: dict[str, str] = {}
    segment_of = {u.id: u.segment for u in catalog.use_cases}
    for cl in catalog.clusters:
        for uid in cl.use_case_ids:
            if uid not in segment_of:
                problems.append(f"Cluster '{cl.id}' references unknown use case '{uid}'.")
            elif segment_of[uid] != cl.segment:
                problems.append(f"Use case '{uid}' segment does not match cluster '{cl.id}'.")
            if uid in seen:
                problems.append(f"Use case '{uid}' is in clusters '{seen[uid]}' and '{cl.id}'.")
            seen[uid] = cl.id
    missing = set(segment_of) - set(seen)
    if missing:
        problems.append(f"Use cases not assigned to any cluster: {sorted(missing)}.")
    return problems


def order_clusters(catalog: UseCaseCatalog) -> UseCaseCatalog:
    """Put the copilot cluster first so researcher slot 0 always covers it."""
    catalog.clusters.sort(key=lambda c: 0 if c.segment == "copilot" else 1)
    return catalog


def save_use_case_catalog(
    use_cases: list[UseCase], clusters: list[UseCaseCluster], tool_context: ToolContext
) -> dict:
    """Validates and saves the catalog of most commonly deployed agent use cases.

    Args:
      use_cases: The ranked list of use cases (about 50), each tagged with a
        segment ('copilot' or 'workflow').
      clusters: Exactly 1 'copilot' cluster plus 4 'workflow' domain clusters;
        every use case must appear in exactly one cluster of the same segment.

    Returns:
      dict with status 'ok' and summary counts, or status 'error' with a list
      of problems to fix before calling again.
    """
    catalog = UseCaseCatalog(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        use_cases=use_cases,
        clusters=clusters,
    )
    problems = validate_catalog(catalog)
    if problems:
        return {"status": "error", "problems": problems}
    catalog = order_clusters(catalog)
    config.ensure_dirs()
    config.USE_CASE_CATALOG.write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
    tool_context.state["use_case_catalog"] = json.loads(catalog.model_dump_json())
    return {
        "status": "ok",
        "use_cases": len(catalog.use_cases),
        "clusters": [f"{c.name} ({len(c.use_case_ids)})" for c in catalog.clusters],
    }
