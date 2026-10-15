"""Deterministic (non-LLM) pipeline nodes implemented as custom ADK agents.

Using code, not prompts, for bookkeeping steps (initialisation, loop gating,
aggregation, publishing) makes the workflow cheaper, testable and
predictable; LLMs are reserved for judgement tasks.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from . import config
from .report import build_report, load_previous, publish
from .schemas import Synthesis
from .tools.evidence import get_store

logger = logging.getLogger(__name__)


def _event(
    agent: BaseAgent,
    ctx: InvocationContext,
    text: str | None = None,
    state_delta: dict[str, Any] | None = None,
    escalate: bool | None = None,
) -> Event:
    return Event(
        author=agent.name,
        invocation_id=ctx.invocation_id,
        content=types.Content(role="model", parts=[types.Part(text=text)]) if text else None,
        actions=EventActions(state_delta=state_delta or {}, escalate=escalate),
    )


def _user_text(ctx: InvocationContext) -> str:
    c = ctx.user_content
    if not c or not c.parts:
        return ""
    return " ".join(p.text or "" for p in c.parts).lower()


def previous_summary(previous: dict[str, Any] | None) -> str:
    """Compact text summary of the last report, used as long-term memory."""
    if not previous:
        return ""
    syn = previous.get("synthesis", {})
    lines = [f"Previous run {previous.get('run_id')} ({previous.get('generated_at')}):"]
    lines.append(syn.get("executive_summary", "")[:1200])
    for seg in syn.get("segments", []):
        drivers = ", ".join(f["factor"] for f in seg.get("top_drivers", []))
        inhib = ", ".join(f["factor"] for f in seg.get("top_inhibitors", []))
        lines.append(f"[{seg['segment']}] drivers: {drivers} | inhibitors: {inhib}")
    return "\n".join(lines)


class RunInitializer(BaseAgent):
    """Assigns a run id and loads long-term memory into session state."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        config.ensure_dirs()
        state = ctx.session.state
        run_id = state.get("requested_run_id") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        refresh = bool(state.get("refresh_scope")) or "refresh scope" in _user_text(ctx)
        # Reset per-run working memory so a re-run in the same session starts clean.
        delta: dict[str, Any] = {
            "run_id": run_id,
            "requested_run_id": None,
            "synthesis": None,
            "critique": "",
        }

        scope_msg = "will be generated"
        if config.USE_CASE_CATALOG.exists() and not refresh:
            delta["use_case_catalog"] = json.loads(
                config.USE_CASE_CATALOG.read_text(encoding="utf-8")
            )
            scope_msg = f"loaded {len(delta['use_case_catalog']['use_cases'])} cached use cases"
        else:
            delta["use_case_catalog"] = None

        previous = load_previous()
        delta["previous_summary"] = previous_summary(previous)
        stats = get_store().stats()
        delta["evidence_before_run"] = stats
        yield _event(
            self,
            ctx,
            text=(
                f"Run {run_id} started. Scope: {scope_msg}. Evidence base: "
                f"{stats['sources_total']} sources / {stats['claims_total']} claims."
            ),
            state_delta=delta,
        )


class MappingGate(BaseAgent):
    """Exits the mapping loop deterministically once every claim is mapped."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        remaining = get_store().count_unmapped()
        if remaining == 0:
            yield _event(self, ctx, text="All claims mapped to root causes.", escalate=True)
        else:
            yield _event(self, ctx, text=f"{remaining} claims awaiting root-cause mapping.")


class EvidenceAggregator(BaseAgent):
    """Builds the bounded evidence digest that the synthesizer reasons over."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        digest = get_store().digest()
        totals = digest["totals"]
        yield _event(
            self,
            ctx,
            text=(
                f"Digest built: {totals['claims_total']} claims from "
                f"{totals['sources_total']} sources in {len(digest['groups'])} groups."
            ),
            state_delta={
                "evidence_digest": json.dumps(digest, ensure_ascii=False),
                "evidence_totals": totals,
            },
        )


class ReportPublisher(BaseAgent):
    """Validates the final synthesis, renders HTML and archives the run."""

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        raw = state.get("synthesis")
        if not raw:
            yield _event(self, ctx, text="No synthesis produced; report not updated.")
            return
        try:
            synthesis = (
                Synthesis.model_validate_json(raw)
                if isinstance(raw, str)
                else Synthesis.model_validate(raw)
            )
        except Exception as e:
            logger.exception("Synthesis failed validation")
            yield _event(self, ctx, text=f"Synthesis failed validation: {e}")
            return

        report = build_report(
            synthesis,
            get_store(),
            run_id=state.get("run_id", "adhoc"),
            catalog=state.get("use_case_catalog"),
            previous=load_previous(),
        )
        paths = publish(report)
        st = report["stats"]
        yield _event(
            self,
            ctx,
            text=(
                f"Report published: {paths['index']}\n"
                f"Sources: {st['sources_total']} ({st.get('sources_new', 0)} new) | "
                f"claims cited: {st['claims_cited']} | "
                f"invalid citations removed: {st['citations_dropped']}\n\n"
                f"{synthesis.executive_summary}"
            ),
            state_delta={"report_path": paths["index"]},
        )
