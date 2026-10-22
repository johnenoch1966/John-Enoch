"""Command-line entry point.

python -m adoption_agent.cli run [--refresh-scope] [--run-id ID]
python -m adoption_agent.cli render      # re-render HTML from data/report.json
python -m adoption_agent.cli stats       # evidence-base statistics
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from google.adk.agents import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from . import config
from .observability import configure_logging, setup_tracing

logger = logging.getLogger("adoption_agent.cli")


async def run_pipeline(
    refresh_scope: bool = False,
    run_id: str | None = None,
    resume: bool = False,
    research: str = "all",
    approval: str | None = None,
) -> int:
    """Run one research cycle, syncing long-term memory with GCS if configured."""
    from .state_sync import download_state, state_bucket, upload_state

    bucket = state_bucket()
    if bucket:
        download_state(bucket)
    try:
        return await _run_cycle(
            refresh_scope, run_id, resume, research, approval or config.APPROVAL_MODE
        )
    finally:
        # Always persist: evidence gathered before a failure is still valuable.
        if bucket:
            upload_state(bucket)


async def _run_cycle(
    refresh_scope: bool, run_id: str | None, resume: bool, research: str, approval: str
) -> int:
    from .agent import app  # import after logging/tracing are configured
    from .hitl import drive_with_approvals, terminal_decider

    runner = Runner(app=app, session_service=InMemorySessionService())
    state = {
        "refresh_scope": refresh_scope,
        "resume": resume,
        "research": research,
        "approval_mode": approval,
    }
    if run_id:
        state["requested_run_id"] = run_id
    session = await runner.session_service.create_session(
        app_name=app.name, user_id="cli", state=state
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text="Run the enterprise agentic AI adoption research cycle.")],
    )
    final_text = ""

    def on_event(event) -> None:
        nonlocal final_text
        if event.content and event.content.parts and not event.partial:
            text = "".join(p.text or "" for p in event.content.parts if not p.thought)
            if text.strip():
                print(f"[{event.author}] {text.strip()[:400]}", flush=True)
                if event.author == "report_publisher":
                    final_text = text

    # ADK caps model calls per invocation at 500 by default. A full cycle
    # (12 researchers with nested search calls + batched mapping + analysts)
    # legitimately needs more, so raise the ceiling while keeping a guard rail.
    await drive_with_approvals(
        runner,
        user_id="cli",
        session_id=session.id,
        new_message=message,
        decide=terminal_decider(approval),
        run_config=RunConfig(max_llm_calls=config.MAX_LLM_CALLS),
        on_event=on_event,
    )
    await runner.close()
    if final_text.startswith("Publication NOT approved"):
        return 0  # a human decision, not a failure
    if not final_text.startswith("Report published"):
        logger.error("Pipeline finished without publishing a report.")
        return 1
    return 0


def render() -> int:
    """Rebuild report views from the stored synthesis and re-render HTML.

    No model calls: useful after changing report logic or the template.
    The run id, timestamp and run-to-run diff of the stored report are kept.
    """
    from .report.builder import build_report, load_history, render_html
    from .schemas import Synthesis
    from .tools.evidence import get_store

    old = json.loads(config.REPORT_JSON.read_text(encoding="utf-8"))
    report = build_report(
        Synthesis.model_validate(old["synthesis"]),
        get_store(),
        run_id=old["run_id"],
        catalog=old.get("catalog"),
        previous=None,
    )
    for key in ("generated_at", "diff"):
        report[key] = old.get(key, report[key])
    for key in ("sources_new", "claims_new"):
        report["stats"][key] = old.get("stats", {}).get(key, 0)
    config.REPORT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    html = render_html(report, load_history())
    out = config.REPORTS_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    (config.REPORTS_DIR / "history" / f"{report['run_id']}.html").write_text(html, encoding="utf-8")
    print(out)
    return 0


def stats() -> int:
    from .tools.evidence import get_store

    print(json.dumps(get_store().stats(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adoption_agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="Run a full research cycle")
    p_run.add_argument("--refresh-scope", action="store_true", help="Rebuild the use-case list")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument(
        "--resume",
        action="store_true",
        help="Skip web research; map, synthesise and publish from stored evidence",
    )
    p_run.add_argument(
        "--research",
        choices=["all", "clusters", "industries", "none"],
        default="all",
        help="Which research fan-outs to run (default: all)",
    )
    p_run.add_argument(
        "--approval",
        choices=["prompt", "auto", "reject"],
        default=None,
        help="Human-in-the-loop policy before publishing (default: prompt, or "
        "$ADOPTION_AGENT_APPROVAL). 'auto' is for unattended scheduled runs.",
    )
    sub.add_parser("render", help="Re-render HTML from data/report.json")
    sub.add_parser("stats", help="Show evidence-base statistics")
    sub.add_parser("publish-draft", help="Publish a reviewed draft (human approval)")
    args = parser.parse_args(argv)

    configure_logging()
    if args.cmd == "run":
        exporters = setup_tracing()
        logger.info("Tracing exporters: %s | model: %s", exporters, config.MODEL)
        return asyncio.run(
            run_pipeline(args.refresh_scope, args.run_id, args.resume, args.research, args.approval)
        )
    if args.cmd == "render":
        return render()
    if args.cmd == "publish-draft":
        from .report import publish_draft

        print(publish_draft()["index"])
        return 0
    return stats()


if __name__ == "__main__":
    sys.exit(main())
