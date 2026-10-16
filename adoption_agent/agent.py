"""Agent graph for the Enterprise Agentic AI Adoption Research Agent.

    adoption_research_pipeline (SequentialAgent)
    ├── run_initializer            custom: run id, load memory (catalog, last report)
    ├── use_case_scoper            LLM + search: top-50 deployed use cases (cached)
    ├── research_fanout            ParallelAgent: 5 researchers, one per use-case cluster
    ├── industry_research_fanout   ParallelAgent: 7 researchers, one per industry
    ├── root_cause_mapping_loop    LoopAgent
    │     ├── mapping_gate         custom: exit when nothing left to map
    │     └── semantic_bias_analyst LLM: root cause + industry tags
    ├── evidence_aggregator        custom: bounded segment + industry digests
    ├── synthesis_fanout           ParallelAgent
    │     ├── segment_analyst      LLM: use cases ranked by impact, drivers/blockers
    │     └── industry_analyst     LLM: per-industry top-5 drivers/blockers/advice
    ├── synthesis_merger           custom: merge into one validated brief
    ├── review_loop                LoopAgent
    │     ├── critic               LLM + validate_citations + exit_loop
    │     └── refiner              LLM, structured output (Synthesis)
    └── report_publisher           custom: validate, diff, render HTML, archive

`adk web` / `adk run` pick up `app` (with the telemetry plugin) from here.
"""

from __future__ import annotations

import json
from typing import Any

from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.exit_loop_tool import exit_loop
from google.genai import types

from . import config, prompts
from .industries import industries_brief, use_case_industries
from .nodes import (
    EvidenceAggregator,
    MappingGate,
    ReportPublisher,
    RunInitializer,
    SynthesisMerger,
)
from .observability import RunTelemetryPlugin
from .schemas import IndustrySynthesis, SegmentSynthesis, Synthesis
from .tools import (
    fetch_page,
    get_unmapped_claims,
    lookup_taxonomy,
    record_evidence,
    record_root_cause_mappings,
    save_use_case_catalog,
    validate_citations,
)
from .tools.evidence import get_store
from .tools.taxonomy import taxonomy_brief

_LOW_TEMP = types.GenerateContentConfig(temperature=0.2)
_INDUSTRY_IDS = ", ".join(config.INDUSTRIES)

# Retry transient failures (quota bursts from the parallel fan-out, 5xx) with
# exponential backoff + jitter instead of failing the whole pipeline.
_RETRY = types.HttpRetryOptions(
    attempts=8,
    initial_delay=4.0,
    max_delay=90.0,
    exp_base=2.0,
    jitter=1.0,
    http_status_codes=[429, 500, 502, 503, 504],
)


def llm(name: str) -> Gemini:
    return Gemini(model=name, retry_options=_RETRY)


FAST_LLM = llm(config.MODEL)
REASONING_LLM = llm(config.REASONING_MODEL)


# --------------------------------------------------------------------------
# Search: built-in Google Search isolated in its own agent, exposed as a tool.
# --------------------------------------------------------------------------
def append_grounding_sources(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """Surface grounding links in the search agent's text.

    Built-in search grounding puts source links in metadata, which the calling
    agent never sees. Appending them lets researchers pass real URLs to
    `fetch_page` instead of guessing.
    """
    gm = llm_response.grounding_metadata
    if not gm or not gm.grounding_chunks or not llm_response.content:
        return None
    lines = ["", "Sources:"]
    for i, chunk in enumerate(gm.grounding_chunks, 1):
        if chunk.web and chunk.web.uri:
            lines.append(f"[{i}] {chunk.web.title or ''} - {chunk.web.uri}")
    if len(lines) > 2:
        llm_response.content.parts = list(llm_response.content.parts or []) + [
            types.Part(text="\n".join(lines))
        ]
    return llm_response


def make_search_tool() -> AgentTool:
    search_agent = LlmAgent(
        name="google_search_agent",
        model=FAST_LLM,
        description="Searches the public web with Google Search and returns "
        "relevant results with source links.",
        instruction=prompts.SEARCH_AGENT,
        tools=[google_search],
        after_model_callback=append_grounding_sources,
    )
    return AgentTool(agent=search_agent)


def _known_urls() -> str:
    known = sorted({c["url"] for c in get_store().all_claims()})
    return "\n".join(f"- {u}" for u in known[:80]) or "(none yet)"


def _skip(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


# --------------------------------------------------------------------------
# Stage 1: scope
# --------------------------------------------------------------------------
def skip_if_catalog_cached(callback_context: CallbackContext) -> types.Content | None:
    if callback_context.state.get("use_case_catalog"):
        return _skip("Using cached use-case catalog (say 'refresh scope' to rebuild).")
    return None


use_case_scoper = LlmAgent(
    name="use_case_scoper",
    model=FAST_LLM,
    description="Identifies the most commonly deployed enterprise agent use cases.",
    instruction=prompts.SCOPER.format(
        n=config.TARGET_USE_CASES, k=config.NUM_WORKFLOW_CLUSTERS, industry_ids=_INDUSTRY_IDS
    ),
    tools=[make_search_tool(), save_use_case_catalog],
    include_contents="none",
    before_agent_callback=skip_if_catalog_cached,
    generate_content_config=_LOW_TEMP,
)


# --------------------------------------------------------------------------
# Stage 2a: parallel research, one researcher per use-case cluster
# --------------------------------------------------------------------------
def _cluster(state: Any, index: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    catalog = state.get("use_case_catalog") or {}
    clusters = catalog.get("clusters", [])
    if index >= len(clusters):
        return None, []
    cl = clusters[index]
    by_id = {u["id"]: u for u in catalog.get("use_cases", [])}
    return cl, [by_id[i] for i in cl["use_case_ids"] if i in by_id]


def make_researcher_instruction(index: int):
    def provider(ctx: ReadonlyContext) -> str:
        cl, use_cases = _cluster(ctx.state, index)
        if cl is None:
            return "There is no cluster for you this run. Reply 'No cluster assigned.'"
        return prompts.RESEARCHER.format(
            cluster_name=cl["name"],
            segment=cl["segment"],
            segment_hint=prompts.COPILOT_HINT
            if cl["segment"] == "copilot"
            else prompts.WORKFLOW_HINT,
            use_cases="\n".join(
                f"- [{u['id']}] {u['name']}: {u['description']}" for u in use_cases
            ),
            known_urls=_known_urls(),
            min_sources=config.MIN_SOURCES_PER_CLUSTER,
            max_sources=config.MAX_SOURCES_PER_CLUSTER,
            industry_ids=_INDUSTRY_IDS,
        )

    return provider


def make_researcher(index: int) -> LlmAgent:
    return LlmAgent(
        name=f"researcher_{index}",
        model=FAST_LLM,
        description=f"Researches adoption drivers/inhibitors for use-case cluster {index}.",
        instruction=make_researcher_instruction(index),
        tools=[make_search_tool(), fetch_page, record_evidence],
        include_contents="none",
        output_key=f"research_notes_{index}",
        generate_content_config=_LOW_TEMP,
    )


def make_scope_gate(kind: str):
    """Skip a research fan-out based on the run's `research` scope.

    research = all | clusters | industries | none   (`--resume` == none)
    """

    def gate(callback_context: CallbackContext) -> types.Content | None:
        scope = callback_context.state.get("research") or "all"
        if callback_context.state.get("resume"):
            scope = "none"
        if scope not in ("all", kind):
            return _skip(f"Skipping {kind} research (research scope: {scope}).")
        return None

    return gate


research_fanout = ParallelAgent(
    name="research_fanout",
    description="Runs one researcher per use-case cluster concurrently.",
    sub_agents=[make_researcher(i) for i in range(config.NUM_CLUSTERS)],
    before_agent_callback=make_scope_gate("clusters"),
)


# --------------------------------------------------------------------------
# Stage 2b: parallel research, one researcher per industry
# --------------------------------------------------------------------------
def make_industry_instruction(industry_id: str):
    name = config.INDUSTRIES[industry_id]
    short = name.split("(")[-1].rstrip(")") if "(" in name else name

    def provider(ctx: ReadonlyContext) -> str:
        catalog = ctx.state.get("use_case_catalog") or {}
        relevant = [
            u for u in catalog.get("use_cases", []) if industry_id in use_case_industries(u)
        ] or catalog.get("use_cases", [])[:15]
        return prompts.INDUSTRY_RESEARCHER.format(
            industry_name=name,
            industry_id=industry_id,
            industry_short=short,
            n=config.TARGET_USE_CASES,
            use_cases="\n".join(f"- [{u['id']}] {u['name']} ({u['segment']})" for u in relevant),
            known_urls=_known_urls(),
            min_sources=max(3, config.MIN_SOURCES_PER_CLUSTER - 1),
            max_sources=config.MAX_SOURCES_PER_CLUSTER,
        )

    return provider


def make_industry_researcher(industry_id: str) -> LlmAgent:
    return LlmAgent(
        name=f"industry_researcher_{industry_id}",
        model=FAST_LLM,
        description=f"Researches agentic AI adoption in {config.INDUSTRIES[industry_id]}.",
        instruction=make_industry_instruction(industry_id),
        tools=[make_search_tool(), fetch_page, record_evidence],
        include_contents="none",
        output_key=f"industry_notes_{industry_id}",
        generate_content_config=_LOW_TEMP,
    )


industry_research_fanout = ParallelAgent(
    name="industry_research_fanout",
    description="Runs one researcher per industry concurrently.",
    sub_agents=[make_industry_researcher(i) for i in config.INDUSTRIES],
    before_agent_callback=make_scope_gate("industries"),
)


# --------------------------------------------------------------------------
# Stage 3: semantic-bias / root-cause + industry tagging loop
# --------------------------------------------------------------------------
semantic_bias_analyst = LlmAgent(
    name="semantic_bias_analyst",
    model=REASONING_LLM,
    description="Maps stated adoption factors to underlying root causes and industries.",
    instruction=prompts.BIAS_ANALYST.format(
        taxonomy=taxonomy_brief(), industries=industries_brief()
    ),
    tools=[get_unmapped_claims, lookup_taxonomy, record_root_cause_mappings],
    include_contents="none",
    generate_content_config=_LOW_TEMP,
)

root_cause_mapping_loop = LoopAgent(
    name="root_cause_mapping_loop",
    description="Maps claims in batches until none remain.",
    sub_agents=[MappingGate(name="mapping_gate"), semantic_bias_analyst],
    max_iterations=config.MAX_MAPPING_ITERATIONS,
)


# --------------------------------------------------------------------------
# Stage 4: parallel synthesis -> merge -> critique/refine loop
# --------------------------------------------------------------------------
def _catalog_lines(state: Any, with_industries: bool = False) -> str:
    catalog = state.get("use_case_catalog") or {}
    lines = []
    for u in catalog.get("use_cases", []):
        line = f"- {u['id']} | {u['name']} | {u['segment']}"
        if with_industries:
            line += " | " + ",".join(use_case_industries(u))
        lines.append(line)
    return "\n".join(lines)


def _as_json(value: Any) -> str:
    if value is None:
        return "{}"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def segment_analyst_instruction(ctx: ReadonlyContext) -> str:
    return prompts.SEGMENT_ANALYST.format(
        taxonomy=taxonomy_brief(),
        scope=_catalog_lines(ctx.state),
        digest=ctx.state.get("evidence_digest", "{}"),
        previous=ctx.state.get("previous_summary", ""),
    )


def industry_analyst_instruction(ctx: ReadonlyContext) -> str:
    return prompts.INDUSTRY_ANALYST.format(
        industries=industries_brief(),
        taxonomy=taxonomy_brief(),
        scope=_catalog_lines(ctx.state, with_industries=True),
        digest=ctx.state.get("industry_digest", "{}"),
    )


def critic_instruction(ctx: ReadonlyContext) -> str:
    return prompts.CRITIC.format(
        synthesis=_as_json(ctx.state.get("synthesis")),
        digest=ctx.state.get("evidence_digest", "{}"),
        industry_digest=ctx.state.get("industry_digest", "{}"),
    )


def refiner_instruction(ctx: ReadonlyContext) -> str:
    return prompts.REFINER.format(
        synthesis=_as_json(ctx.state.get("synthesis")),
        critique=ctx.state.get("critique", ""),
        digest=ctx.state.get("evidence_digest", "{}"),
        industry_digest=ctx.state.get("industry_digest", "{}"),
    )


def skip_if_no_evidence(callback_context: CallbackContext) -> types.Content | None:
    totals = callback_context.state.get("evidence_totals") or {}
    if not totals.get("claims_total"):
        return _skip("No evidence collected; skipping synthesis.")
    return None


segment_analyst = LlmAgent(
    name="segment_analyst",
    model=REASONING_LLM,
    description="Ranks copilot and workflow use cases by impact; drivers and blockers.",
    instruction=segment_analyst_instruction,
    output_schema=SegmentSynthesis,
    output_key="synthesis_segments",
    include_contents="none",
    generate_content_config=_LOW_TEMP,
)

industry_analyst = LlmAgent(
    name="industry_analyst",
    model=REASONING_LLM,
    description="Builds per-industry top-5 drivers, blockers and advice.",
    instruction=industry_analyst_instruction,
    output_schema=IndustrySynthesis,
    output_key="synthesis_industries",
    include_contents="none",
    generate_content_config=_LOW_TEMP,
)

synthesis_fanout = ParallelAgent(
    name="synthesis_fanout",
    description="Segment and industry analysts work concurrently on separate digests.",
    sub_agents=[segment_analyst, industry_analyst],
    before_agent_callback=skip_if_no_evidence,
)

critic = LlmAgent(
    name="critic",
    model=REASONING_LLM,
    description="Validates citations, coverage and reasoning of the draft.",
    instruction=critic_instruction,
    tools=[validate_citations, exit_loop],
    output_key="critique",
    include_contents="none",
    before_agent_callback=skip_if_no_evidence,
    generate_content_config=_LOW_TEMP,
)

refiner = LlmAgent(
    name="refiner",
    model=REASONING_LLM,
    description="Revises the draft according to the critique.",
    instruction=refiner_instruction,
    output_schema=Synthesis,
    output_key="synthesis",
    include_contents="none",
    before_agent_callback=skip_if_no_evidence,
    generate_content_config=_LOW_TEMP,
)

review_loop = LoopAgent(
    name="review_loop",
    description="Critic/refiner loop until the brief passes review.",
    sub_agents=[critic, refiner],
    max_iterations=config.MAX_REVIEW_ITERATIONS,
)


# --------------------------------------------------------------------------
# Root
# --------------------------------------------------------------------------
root_agent = SequentialAgent(
    name="adoption_research_pipeline",
    description=(
        "Researches public reports on what drives and inhibits enterprise "
        "adoption of agentic AI (copilot assistants vs workflow agents) by use "
        "case and industry, analyses semantic bias, and publishes an updatable "
        "HTML report."
    ),
    sub_agents=[
        RunInitializer(name="run_initializer"),
        use_case_scoper,
        research_fanout,
        industry_research_fanout,
        root_cause_mapping_loop,
        EvidenceAggregator(name="evidence_aggregator"),
        synthesis_fanout,
        SynthesisMerger(name="synthesis_merger"),
        review_loop,
        ReportPublisher(name="report_publisher"),
    ],
)

app = App(name="adoption_agent", root_agent=root_agent, plugins=[RunTelemetryPlugin()])
