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
) -> int:
    from .agent import app  # import after logging/tracing are configured

    runner = Runner(app=app, session_service=InMemorySessionService())
    state = {"refresh_scope": refresh_scope, "resume": resume, "research": research}
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
    # ADK caps model calls per invocation at 500 by default. A full cycle
    # (12 researchers with nested search calls + batched mapping + analysts)
    # legitimately needs more, so raise the ceiling while keeping a guard rail.
    run_config = RunConfig(max_llm_calls=config.MAX_LLM_CALLS)
    async for event in runner.run_async(
        user_id="cli", session_id=session.id, new_message=message, run_config=run_config
    ):
        if event.content and event.content.parts and not event.partial:
            text = "".join(p.text or "" for p in event.content.parts if not p.thought)
            if text.strip():
                print(f"[{event.author}] {text.strip()[:400]}", flush=True)
                if event.author == "report_publisher":
                    final_text = text
    await runner.close()
    if not final_text.startswith("Report published"):
        logger.error("Pipeline finished without publishing a report.")
        return 1
    return 0


def render() -> int:
    from .report.builder import load_history, render_html

    report = json.loads(config.REPORT_JSON.read_text(encoding="utf-8"))
    out = config.REPORTS_DIR / "index.html"
    out.write_text(render_html(report, load_history()), encoding="utf-8")
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
    sub.add_parser("render", help="Re-render HTML from data/report.json")
    sub.add_parser("stats", help="Show evidence-base statistics")
    args = parser.parse_args(argv)

    configure_logging()
    if args.cmd == "run":
        exporters = setup_tracing()
        logger.info("Tracing exporters: %s | model: %s", exporters, config.MODEL)
        return asyncio.run(
            run_pipeline(args.refresh_scope, args.run_id, args.resume, args.research)
        )
    if args.cmd == "render":
        return render()
    return stats()


if __name__ == "__main__":
    sys.exit(main())
