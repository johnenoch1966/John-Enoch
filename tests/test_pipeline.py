"""Offline end-to-end test of the full agent graph with a scripted model.

A deterministic fake LLM plays every role (search, researchers, bias
analyst, synthesizer, critic) so we can verify orchestration, tool wiring,
state hand-offs, loop termination, report publishing and telemetry without
network access or model cost.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from adoption_agent import config
from tests.samples import (
    sample_catalog,
    sample_industry_synthesis,
    sample_segment_synthesis,
    sample_synthesis,
)


def _si_text(req: LlmRequest) -> str:
    si = req.config.system_instruction if req.config else None
    if si is None:
        return ""
    if isinstance(si, str):
        return si
    if isinstance(si, types.Content):
        return "".join(p.text or "" for p in si.parts or [])
    return str(si)


def _call(name: str, args: dict) -> types.Content:
    return types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]
    )


def _text(t: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=t)])


class ScriptedLlm(BaseLlm):
    model: str = "scripted-fake"

    @property
    def capabilities(self):
        from google.adk.models._capabilities import LlmCapabilities

        return LlmCapabilities(output_schema_and_tools=True)

    def _respond(self, req: LlmRequest) -> types.Content:
        si = _si_text(req)
        last = req.contents[-1] if req.contents else None
        frs = [p.function_response for p in (last.parts if last else []) if p.function_response]
        fr = frs[-1] if frs else None

        if "web research specialist" in si:
            return _text("- Agentic AI survey | Example Research | 2025 | governance gaps")

        if "research analyst investigating" in si:
            if fr is None:
                industry = re.search(r"Industry: (.+?)\s+\(id: (\w+)\)", si)
                if industry:  # industry researcher
                    name, segment, industries = industry.group(1), "workflow", [industry.group(2)]
                else:  # use-case cluster researcher
                    cluster = re.search(r"Cluster: (.+?)\s+\(segment: (\w+)\)", si)
                    name, segment, industries = cluster.group(1), cluster.group(2), []
                slug = re.sub(r"\W+", "-", name.lower())
                return _call(
                    "record_evidence",
                    {
                        "source": {
                            "url": f"https://example.org/{slug}",
                            "title": f"{name} adoption report",
                            "publisher": "Example Research",
                            "published": "2025-09",
                            "source_type": "analyst",
                            "vendor_affiliated": False,
                            "methodology": "survey",
                        },
                        "claims": [
                            {
                                "stated_factor": "Access to connectors not enabled",
                                "polarity": "inhibitor",
                                "segment": segment,
                                "use_case_ids": ["uc-0"],
                                "industries": industries,
                                "evidence_quote": "Admins have not enabled connectors.",
                            },
                            {
                                "stated_factor": "Executive sponsorship",
                                "polarity": "driver",
                                "segment": segment,
                                "use_case_ids": ["uc-10"],
                                "industries": industries,
                                "evidence_quote": "Sponsored programmes scaled faster.",
                            },
                        ],
                    },
                )
            return _text("- Connectors disabled is the top inhibitor.")

        if "semantic-bias analyst" in si:
            if fr is None:
                return _call("get_unmapped_claims", {})
            if fr.name == "get_unmapped_claims":
                mappings = [
                    {
                        "claim_id": c["id"],
                        "root_cause_ids": [
                            "risk_auditability"
                            if "connector" in c["stated_factor"].lower()
                            else "operating_model"
                        ],
                        "inference": "scripted",
                        "confidence": "high",
                        "industries": json.loads(c["researcher_industries"] or "[]")
                        or ["cross_industry"],
                    }
                    for c in fr.response["claims"]
                ]
                return _call("record_root_cause_mappings", {"mappings": mappings})
            return _text("Mapped batch.")

        ids = sorted({int(i) for i in re.findall(r'"claim_id": (\d+)', si)})[:3]
        if "principal analyst producing" in si:
            return _text(sample_segment_synthesis(ids).model_dump_json())
        if "industry practice lead" in si:
            return _text(sample_industry_synthesis(ids).model_dump_json())
        if "revising the research brief" in si:
            return _text(sample_synthesis(ids).model_dump_json())

        if "demanding research reviewer" in si:
            if fr is None:
                ids = sorted(
                    {
                        int(i)
                        for i in re.findall(
                            r"\d+", " ".join(re.findall(r'"evidence_ids": \[([^\]]*)\]', si))
                        )
                    }
                )
                return _call("validate_citations", {"evidence_ids": ids})
            if fr.name == "validate_citations":
                return _call("exit_loop", {})
            return _text("APPROVED")

        return _text("ok")

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=self._respond(llm_request),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=100, candidates_token_count=10, total_token_count=110
            ),
        )


def _patch_models(agent, fake: BaseLlm) -> None:
    if isinstance(agent, LlmAgent):
        agent.model = fake
        for t in agent.tools:
            if isinstance(t, AgentTool):
                _patch_models(t.agent, fake)
    for sub in agent.sub_agents:
        _patch_models(sub, fake)


async def _run(app) -> list[str]:
    runner = Runner(app=app, session_service=InMemorySessionService())
    session = await runner.session_service.create_session(
        app_name=app.name, user_id="t", state={"requested_run_id": "test-run"}
    )
    authors = []
    async for ev in runner.run_async(
        user_id="t",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="go")]),
    ):
        authors.append(ev.author)
    await runner.close()
    return authors


async def test_full_pipeline_offline(tmp_home):
    # Cached scope -> scoper is skipped via its before_agent_callback.
    config.USE_CASE_CATALOG.write_text(sample_catalog().model_dump_json())

    from adoption_agent.agent import app, root_agent

    _patch_models(root_agent, ScriptedLlm())
    authors = await _run(app)

    # Every stage ran, in order.
    for name in [
        "run_initializer",
        "use_case_scoper",
        "researcher_0",
        "researcher_4",
        "industry_researcher_fsi",
        "industry_researcher_energy_utilities",
        "mapping_gate",
        "semantic_bias_analyst",
        "evidence_aggregator",
        "segment_analyst",
        "industry_analyst",
        "synthesis_merger",
        "critic",
        "report_publisher",
    ]:
        assert name in authors, name
    assert "refiner" not in authors  # critic approved on the first pass

    n_sources = config.NUM_CLUSTERS + len(config.INDUSTRIES)
    report = json.loads(config.REPORT_JSON.read_text())
    assert report["run_id"] == "test-run"
    assert report["stats"]["sources_total"] == n_sources
    assert report["stats"]["claims_total"] == 2 * n_sources
    assert report["stats"]["mapped_total"] == 2 * n_sources
    assert report["stats"]["citations_dropped"] == 0
    assert "executive_summary" not in report["synthesis"]
    assert report["segment_views"]["copilot"]["ranked"][0]["use_case_id"] == "uc-0"
    fsi = next(v for v in report["industry_views"] if v["id"] == "fsi")
    assert fsi["claim_count"] == 2 and len(fsi["profile"]["drivers"]) == 5
    html = (config.REPORTS_DIR / "index.html").read_text()
    assert "Industry deep-dive" in html and "Top AI use cases by industry" in html

    metrics = json.loads((config.RUNS_DIR / "test-run" / "metrics.json").read_text())
    assert metrics["totals"]["model_calls"] > 10
    assert metrics["tools"]["record_evidence"]["calls"] == n_sources
    assert (config.RUNS_DIR / "test-run" / "events.jsonl").exists()

    # Second run: evidence accumulates (deduplicated), diff is computed.
    authors2 = await _run(app)
    assert "report_publisher" in authors2
    report2 = json.loads(config.REPORT_JSON.read_text())
    assert report2["stats"]["sources_total"] == n_sources  # deduped
    assert report2["diff"]["first_run"] is False
    history = (config.DATA_DIR / "history.jsonl").read_text().splitlines()
    assert len(history) == 2
