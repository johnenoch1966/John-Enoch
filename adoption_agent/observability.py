"""Observability: structured logs, per-run metrics and OpenTelemetry setup.

`RunTelemetryPlugin` is an ADK plugin, so it observes *every* agent, model
call and tool call in the tree without touching agent code. It:
  * emits one structured JSON log line per lifecycle event (Cloud Logging
    friendly: a single `jsonPayload` per line),
  * aggregates latency, token usage, tool success/error counts per agent,
  * writes `runs/<run_id>/events.jsonl` and `runs/<run_id>/metrics.json`.

`setup_tracing()` wires ADK's built-in OpenTelemetry spans to Cloud Trace
and/or any OTLP endpoint, and to a local SQLite span store for offline
inspection.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin

from . import config
from .privacy import redact_regex

logger = logging.getLogger("adoption_agent.telemetry")


def _safe(obj: Any, limit: int = 300) -> Any:
    """Mask PII and truncate large values so logs stay safe and readable."""
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    s = redact_regex(s)[0]
    return s if len(s) <= limit else s[:limit] + f"...(+{len(s) - limit} chars)"


class RunTelemetryPlugin(BasePlugin):
    def __init__(self, name: str = "run_telemetry") -> None:
        super().__init__(name=name)
        self._depth = 0
        self._reset()

    def _reset(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()
        self._agent_t: dict[tuple[str, str], float] = {}
        self._tool_t: dict[str, float] = {}
        self._model_t: dict[str, float] = {}
        self.agents: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.tools: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.errors: list[dict[str, Any]] = []

    def _emit(self, kind: str, **fields: Any) -> None:
        rec = {
            "ts": round(time.time(), 3),
            "elapsed_s": round(time.perf_counter() - self._t0, 3),
            "event": kind,
            **fields,
        }
        self.events.append(rec)
        logger.info(json.dumps(rec, default=str))

    # ---------------------------------------------------------------- run
    # AgentTool executes its wrapped agent in a nested Runner that inherits the
    # app's plugins, so run callbacks fire for every search call as well. Track
    # nesting depth so only the outermost run resets and writes metrics.
    async def before_run_callback(self, *, invocation_context) -> None:
        if self._depth == 0:
            self._reset()
            self._emit("run_start", invocation_id=invocation_context.invocation_id)
        else:
            self._emit("nested_run_start", invocation_id=invocation_context.invocation_id)
        self._depth += 1
        return None

    async def after_run_callback(self, *, invocation_context) -> None:
        self._depth = max(0, self._depth - 1)
        if self._depth > 0:
            return None
        state = invocation_context.session.state
        run_id = state.get("run_id") or invocation_context.invocation_id
        summary = self.summary()
        self._emit("run_end", run_id=run_id, **summary["totals"])
        self._write(run_id, summary)
        return None

    async def on_run_error_callback(self, *, invocation_context, error) -> None:
        self.errors.append({"scope": "run", "error": repr(error)})
        self._emit("run_error", error=repr(error))
        return None

    # -------------------------------------------------------------- agent
    async def before_agent_callback(self, *, agent, callback_context):
        self._agent_t[(callback_context.invocation_id, agent.name)] = time.perf_counter()
        self._emit("agent_start", agent=agent.name)
        return None

    async def after_agent_callback(self, *, agent, callback_context):
        t = self._agent_t.pop((callback_context.invocation_id, agent.name), None)
        dur = time.perf_counter() - t if t else 0.0
        self.agents[agent.name]["runs"] += 1
        self.agents[agent.name]["seconds"] += dur
        self._emit("agent_end", agent=agent.name, duration_s=round(dur, 2))
        return None

    async def on_agent_error_callback(self, *, agent, callback_context, error):
        self.errors.append({"scope": "agent", "agent": agent.name, "error": repr(error)})
        self._emit("agent_error", agent=agent.name, error=repr(error))
        return None

    # -------------------------------------------------------------- model
    async def before_model_callback(self, *, callback_context, llm_request):
        self._model_t[callback_context.agent_name] = time.perf_counter()
        self.agents[callback_context.agent_name]["model_calls"] += 1
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        name = callback_context.agent_name
        t = self._model_t.pop(name, None)
        dur = time.perf_counter() - t if t else 0.0
        u = llm_response.usage_metadata
        prompt = (u.prompt_token_count or 0) if u else 0
        output = (u.candidates_token_count or 0) if u else 0
        a = self.agents[name]
        a["prompt_tokens"] += prompt
        a["output_tokens"] += output
        a["model_seconds"] += dur
        self._emit(
            "model_response",
            agent=name,
            latency_s=round(dur, 2),
            prompt_tokens=prompt,
            output_tokens=output,
            grounded=bool(llm_response.grounding_metadata),
            error=llm_response.error_code,
        )
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        self.errors.append(
            {"scope": "model", "agent": callback_context.agent_name, "error": repr(error)}
        )
        self._emit("model_error", agent=callback_context.agent_name, error=repr(error))
        return None

    # --------------------------------------------------------------- tool
    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        self._tool_t[tool_context.function_call_id or tool.name] = time.perf_counter()
        self._emit(
            "tool_start", agent=tool_context.agent_name, tool=tool.name, args=_safe(tool_args)
        )
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        t = self._tool_t.pop(tool_context.function_call_id or tool.name, None)
        dur = time.perf_counter() - t if t else 0.0
        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        m = self.tools[tool.name]
        m["calls"] += 1
        m["seconds"] += dur
        m["errors"] += int(status == "error")
        self._emit(
            "tool_end",
            agent=tool_context.agent_name,
            tool=tool.name,
            status=status,
            latency_s=round(dur, 2),
            result=_safe(result, 200),
        )
        return None

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        self.tools[tool.name]["exceptions"] += 1
        self.errors.append({"scope": "tool", "tool": tool.name, "error": repr(error)})
        self._emit("tool_error", tool=tool.name, error=repr(error))
        # Return the error to the model so it can recover instead of crashing.
        return {"status": "error", "error_message": f"{type(error).__name__}: {error}"}

    # ------------------------------------------------------------ outputs
    def summary(self) -> dict[str, Any]:
        agents = {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in self.agents.items()}
        tools = {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in self.tools.items()}
        return {
            "totals": {
                "wall_seconds": round(time.perf_counter() - self._t0, 1),
                "model_calls": int(sum(a.get("model_calls", 0) for a in agents.values())),
                "prompt_tokens": int(sum(a.get("prompt_tokens", 0) for a in agents.values())),
                "output_tokens": int(sum(a.get("output_tokens", 0) for a in agents.values())),
                "tool_calls": int(sum(t.get("calls", 0) for t in tools.values())),
                "tool_errors": int(sum(t.get("errors", 0) for t in tools.values())),
                "exceptions": len(self.errors),
            },
            "agents": agents,
            "tools": tools,
            "errors": self.errors,
        }

    def _write(self, run_id: str, summary: dict[str, Any]) -> None:
        try:
            d = config.RUNS_DIR / str(run_id)
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "events.jsonl", "w", encoding="utf-8") as f:
                for e in self.events:
                    f.write(json.dumps(e, default=str) + "\n")
            (d / "metrics.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        except OSError as e:  # never fail a run because telemetry could not be written
            logger.warning("Could not write run telemetry: %s", e)


def configure_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Quieten noisy libraries.
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def setup_tracing() -> list[str]:
    """Enable OpenTelemetry exporters for ADK's built-in spans.

    * ADOPTION_AGENT_TRACE_TO_CLOUD=true -> Cloud Trace (+ Cloud Monitoring)
    * OTEL_EXPORTER_OTLP_ENDPOINT=...    -> any OTLP collector (Jaeger, etc.)
    * always                              -> runs/traces.db (local SQLite)

    Returns the list of enabled exporter names.
    """
    from google.adk.telemetry.setup import OTelHooks, maybe_set_otel_providers
    from google.adk.telemetry.sqlite_span_exporter import SqliteSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    enabled: list[str] = []
    hooks: list[OTelHooks] = []
    config.ensure_dirs()
    hooks.append(
        OTelHooks(
            span_processors=[
                BatchSpanProcessor(SqliteSpanExporter(db_path=str(config.RUNS_DIR / "traces.db")))
            ]
        )
    )
    enabled.append("sqlite")
    if os.getenv("ADOPTION_AGENT_TRACE_TO_CLOUD", "").lower() in ("1", "true", "yes"):
        try:
            from google.adk.telemetry.google_cloud import get_gcp_exporters

            hooks.append(get_gcp_exporters(enable_cloud_tracing=True, enable_cloud_metrics=True))
            enabled.append("cloud_trace")
        except Exception as e:  # pragma: no cover - depends on GCP env
            logger.warning("Cloud Trace exporter unavailable: %s", e)
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        enabled.append("otlp")
    maybe_set_otel_providers(otel_hooks_to_setup=hooks)
    return enabled
