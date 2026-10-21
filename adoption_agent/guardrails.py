"""Guardrail plugin: PII redaction for every model call and tool result.

As an ADK plugin it applies globally to every agent in the tree, including the
nested search agents run by `AgentTool`, without changing any agent code:

* `before_model_callback` masks PII in all text and function-response parts
  of the outgoing `LlmRequest`, so personal data never reaches the model.
* `after_tool_callback` masks PII in tool results (fetched pages, search
  results, store queries) before the model sees them.

Every redaction is counted by type and agent, logged, and written to
`runs/<run_id>/privacy.json` as an auditable record.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from . import config
from .privacy import redact_obj, redact_regex

logger = logging.getLogger("adoption_agent.privacy")


class PiiRedactionPlugin(BasePlugin):
    def __init__(self, name: str = "pii_redaction") -> None:
        super().__init__(name=name)
        self._depth = 0
        self._reset()

    def _reset(self) -> None:
        self.by_type: Counter[str] = Counter()
        self.by_agent: dict[str, Counter[str]] = defaultdict(Counter)
        self.model_requests_scanned = 0
        self.tool_results_scanned = 0

    def _record(self, agent: str, counts: Counter[str], where: str) -> None:
        if not counts:
            return
        self.by_type.update(counts)
        self.by_agent[agent].update(counts)
        logger.info(json.dumps({"event": "pii_redacted", "agent": agent, "where": where, **counts}))

    # ------------------------------------------------------------ run scope
    async def before_run_callback(self, *, invocation_context) -> None:
        if self._depth == 0:
            self._reset()
        self._depth += 1
        return None

    async def after_run_callback(self, *, invocation_context) -> None:
        self._depth = max(0, self._depth - 1)
        if self._depth:
            return None
        run_id = invocation_context.session.state.get("run_id") or invocation_context.invocation_id
        summary = self.summary()
        logger.info(json.dumps({"event": "pii_summary", "run_id": run_id, **summary["totals"]}))
        try:
            d = config.RUNS_DIR / str(run_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "privacy.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("Could not write privacy audit: %s", e)
        return None

    # ---------------------------------------------------------------- model
    async def before_model_callback(self, *, callback_context, llm_request):
        self.model_requests_scanned += 1
        counts: Counter[str] = Counter()
        for content in llm_request.contents or []:
            for part in content.parts or []:
                if part.text:
                    part.text, c = redact_regex(part.text)
                    counts.update(c)
                if part.function_response and part.function_response.response:
                    part.function_response.response, c = redact_obj(part.function_response.response)
                    counts.update(c)
        self._record(callback_context.agent_name, counts, "model_request")
        return None  # modified in place; never block the call

    # ----------------------------------------------------------------- tool
    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        self.tool_results_scanned += 1
        masked, counts = redact_obj(result)
        self._record(tool_context.agent_name, counts, f"tool:{tool.name}")
        return masked if counts else None

    # -------------------------------------------------------------- outputs
    def summary(self) -> dict[str, Any]:
        return {
            "totals": {
                "pii_items_masked": int(sum(self.by_type.values())),
                "model_requests_scanned": self.model_requests_scanned,
                "tool_results_scanned": self.tool_results_scanned,
            },
            "by_type": dict(self.by_type),
            "by_agent": {k: dict(v) for k, v in self.by_agent.items()},
        }
