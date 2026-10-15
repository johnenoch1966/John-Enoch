from __future__ import annotations

import json
from types import SimpleNamespace

from adoption_agent import config
from adoption_agent.observability import RunTelemetryPlugin


def _ctx(inv: str, state: dict | None = None):
    return SimpleNamespace(invocation_id=inv, session=SimpleNamespace(state=state or {}))


async def test_nested_runs_do_not_reset_or_write_metrics(tmp_home):
    """AgentTool runs its agent in a nested Runner that shares app plugins."""
    p = RunTelemetryPlugin()
    await p.before_run_callback(invocation_context=_ctx("outer"))
    p.tools["record_evidence"]["calls"] += 3

    await p.before_run_callback(invocation_context=_ctx("nested"))
    await p.after_run_callback(invocation_context=_ctx("nested", {"run_id": "nested"}))
    assert p.tools["record_evidence"]["calls"] == 3  # not reset by nested run
    assert not (config.RUNS_DIR / "nested").exists()  # nested run does not write

    await p.after_run_callback(invocation_context=_ctx("outer", {"run_id": "r1"}))
    metrics = json.loads((config.RUNS_DIR / "r1" / "metrics.json").read_text())
    assert metrics["totals"]["tool_calls"] == 3
    kinds = [
        json.loads(line)["event"]
        for line in (config.RUNS_DIR / "r1" / "events.jsonl").read_text().splitlines()
    ]
    assert kinds[0] == "run_start" and "nested_run_start" in kinds and kinds[-1] == "run_end"
