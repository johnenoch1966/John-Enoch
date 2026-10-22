"""Human-in-the-loop: approval gate before the report is published.

Publishing replaces the PUBLIC report, so it is the one irreversible,
outward-facing action in the pipeline. It is guarded by an ADK tool
confirmation:

* `request_publication_approval` is a FunctionTool with
  ``require_confirmation``. When a human must decide, ADK pauses the
  (resumable) invocation and emits an ``adk_request_confirmation`` event; the
  run resumes when the human approves or rejects.
* `adk web` renders the confirmation natively; the CLI prompts in the
  terminal (see `drive_with_approvals`).
* Approval policy (`approval_mode` in session state or ADOPTION_AGENT_APPROVAL):
    prompt  (default) a human must approve; non-interactive runs reject
    auto    unattended runs (e.g. a scheduler) approve automatically; the
            GitHub refresh workflow then opens a pull request so a human still
            reviews the change before it goes live
    reject  never publish; save a draft for later review (`cli publish-draft`)
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from google.adk.tools import FunctionTool, ToolContext
from google.genai import types

from . import config

logger = logging.getLogger(__name__)

CONFIRMATION_CALL = "adk_request_confirmation"


def approval_mode(state: Any) -> str:
    return str(state.get("approval_mode") or config.APPROVAL_MODE).lower()


def request_publication_approval(summary: str, tool_context: ToolContext) -> dict:
    """Asks a human to approve publishing the research brief to the public report.

    Call exactly once, after the brief has passed review, with a short
    summary of what will be published.

    Args:
      summary: 3-6 bullet points: segment headlines, top changes versus the
        previous report, and the size of the evidence base.

    Returns:
      dict with status 'approved' when publication may proceed. If the human
      rejects, the call returns an error and the report is kept as a draft.
    """
    tool_context.state["publication_approved"] = True
    tool_context.state["approval_summary"] = summary
    how = "auto-approved by policy" if approval_mode(tool_context.state) == "auto" else "human"
    tool_context.state["approval_decided_by"] = how
    return {"status": "approved", "decided_by": how}


def _approval_required(summary: str, tool_context: ToolContext) -> bool:
    """Confirmation is required unless the run's policy is 'auto'."""
    return approval_mode(tool_context.state) != "auto"


publication_approval_tool = FunctionTool(
    request_publication_approval, require_confirmation=_approval_required
)


# ---------------------------------------------------------------------------
# Client side: drive a run and answer confirmation requests
# ---------------------------------------------------------------------------
def pending_confirmations(event: Any) -> list[types.FunctionCall]:
    if not event.content or not event.content.parts:
        return []
    return [
        p.function_call
        for p in event.content.parts
        if p.function_call and p.function_call.name == CONFIRMATION_CALL
    ]


def terminal_decider(mode: str) -> Callable[[dict], bool]:
    """Decision function for the CLI: prompt a human when possible."""

    def decide(request_args: dict) -> bool:
        if mode == "reject":
            return False
        original = request_args.get("originalFunctionCall", {}) or {}
        summary = (original.get("args") or {}).get("summary", "(no summary)")
        print("\n" + "=" * 70 + "\nHUMAN APPROVAL REQUIRED: publish the updated report?\n")
        print(summary)
        print("=" * 70)
        if not sys.stdin.isatty():
            print("Non-interactive session: rejecting (draft will be saved).")
            return False
        return input("Approve publication? [y/N] ").strip().lower() in ("y", "yes")

    return decide


async def drive_with_approvals(
    runner: Any,
    *,
    user_id: str,
    session_id: str,
    new_message: types.Content,
    decide: Callable[[dict], bool],
    run_config: Any = None,
    on_event: Callable[[Any], None] | None = None,
) -> None:
    """Run to completion, pausing for human decisions whenever ADK asks."""
    message: types.Content | None = new_message
    invocation_id: str | None = None
    while True:
        pending: list[types.FunctionCall] = []
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
            invocation_id=invocation_id,
            run_config=run_config,
        ):
            if on_event:
                on_event(event)
            calls = pending_confirmations(event)
            if calls:
                pending.extend(calls)
                invocation_id = event.invocation_id
        if not pending:
            return
        parts = []
        for call in pending:
            approved = bool(decide(dict(call.args or {})))
            logger.info("Human decision for %s: %s", call.id, approved)
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call.id, name=CONFIRMATION_CALL, response={"confirmed": approved}
                    )
                )
            )
        message = types.Content(role="user", parts=parts)
