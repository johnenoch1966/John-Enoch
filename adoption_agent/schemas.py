"""Typed contracts shared by tools, agents and the report renderer.

These Pydantic models are used three ways:
  * as tool parameter types (ADK turns them into function-call JSON schemas
    and validates the model's arguments before the tool runs),
  * as ``output_schema`` for structured LLM output (synthesis / refiner),
  * as the on-disk format for ``data/*.json``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Segment = Literal["copilot", "workflow"]
Polarity = Literal["driver", "inhibitor"]
Confidence = Literal["low", "medium", "high"]
# Keep in sync with config.INDUSTRIES (asserted in tests).
IndustryId = Literal[
    "fsi", "hcls", "manufacturing", "retail_cpg", "tmt", "public_sector", "energy_utilities"
]
IndustryTag = Literal[
    "fsi",
    "hcls",
    "manufacturing",
    "retail_cpg",
    "tmt",
    "public_sector",
    "energy_utilities",
    "cross_industry",
]
AdoptionStage = Literal["experimenting", "piloting", "scaling", "mainstream"]
SourceType = Literal[
    "analyst",  # Gartner, Forrester, IDC, ...
    "consultancy",  # McKinsey, BCG, Deloitte, Accenture, ...
    "vendor",  # Microsoft, Google, Anthropic, Salesforce, ServiceNow, ...
    "academic",
    "regulator",  # central banks, FCA, EU, NIST, ...
    "industry_body",
    "media",
    "other",
]


# --------------------------------------------------------------------------
# Scope: the top-N use-case catalog
# --------------------------------------------------------------------------
class UseCase(BaseModel):
    id: str = Field(description="Short kebab-case slug, e.g. 'kyc-onboarding'.")
    name: str
    segment: Segment = Field(
        description="'copilot' = horizontal desktop assistant usage (Gemini, "
        "Microsoft Copilot, Claude, ChatGPT Enterprise); 'workflow' = an agent "
        "embedded in a specific business process."
    )
    domain: str = Field(description="Business domain, e.g. 'Customer Service'.")
    description: str
    example_industries: list[str] = Field(default_factory=list)
    industries: list[IndustryId] = Field(
        default_factory=list,
        description="Canonical industries where this use case is most deployed.",
    )
    prevalence_evidence: str = Field(
        description="One sentence on why this is among the most deployed."
    )


class UseCaseCluster(BaseModel):
    id: str = Field(description="Slug, e.g. 'customer-service'.")
    name: str
    segment: Segment
    use_case_ids: list[str]


class UseCaseCatalog(BaseModel):
    generated_at: str = ""
    use_cases: list[UseCase]
    clusters: list[UseCaseCluster]


# --------------------------------------------------------------------------
# Evidence: sources + the factor claims extracted from them
# --------------------------------------------------------------------------
class SourceInput(BaseModel):
    url: str = Field(description="Final (resolved) URL of the report or article.")
    title: str
    publisher: str = Field(description="Organisation that published it.")
    published: str = Field(default="unknown", description="Publication date as YYYY or YYYY-MM.")
    source_type: SourceType = "other"
    vendor_affiliated: bool = Field(
        default=False,
        description="True if published or sponsored by a vendor that sells "
        "agentic AI products (a potential source of framing bias).",
    )
    methodology: str = Field(
        default="",
        description="e.g. 'survey of 1,100 IT leaders', 'case study', 'opinion'.",
    )


class ClaimInput(BaseModel):
    stated_factor: str = Field(
        description="The factor exactly as the source frames it (short phrase)."
    )
    polarity: Polarity
    segment: Segment
    use_case_ids: list[str] = Field(default_factory=list)
    evidence_quote: str = Field(
        description="Verbatim or near-verbatim supporting sentence from the source."
    )
    statistic: str = Field(default="", description="Any number attached, e.g. '62% cite security'.")
    industries: list[IndustryTag] = Field(
        default_factory=list,
        description="Industries the claim is about; ['cross_industry'] if generic.",
    )


class RootCauseMappingInput(BaseModel):
    claim_id: int
    root_cause_ids: list[str] = Field(
        description="One or more taxonomy category ids, most likely first."
    )
    inference: str = Field(description="Why the stated factor maps to this underlying cause.")
    confidence: Confidence = "medium"
    framing_bias: str = Field(
        default="",
        description="How the source's framing may distort the factor (e.g. a "
        "vendor reframing a governance gap as a 'training' issue). Empty if none.",
    )
    industries: list[IndustryTag] = Field(
        description="Industries this claim applies to, judged from the source, "
        "publisher and quote. Use ['cross_industry'] if not industry-specific."
    )


# --------------------------------------------------------------------------
# Synthesis: structured LLM output that drives the HTML report
# --------------------------------------------------------------------------
class RankedFactor(BaseModel):
    factor: str = Field(description="Short label, max 8 words.")
    root_cause_id: str
    explanation: str = Field(description="One sentence, max 20 words.")
    strength: Literal["weak", "moderate", "strong"]
    evidence_ids: list[int] = Field(description="Claim ids from the digest.")


class Approach(BaseModel):
    approach: str = Field(description="Short label, max 10 words.")
    rationale: str = Field(description="One sentence, max 20 words.")
    evidence_ids: list[int]


class UseCaseImpact(BaseModel):
    use_case_id: str = Field(description="Id from the use-case catalog.")
    impact_score: int = Field(
        ge=1, le=5, description="Business impact realised by adopters, 1 (low) to 5 (high)."
    )
    adoption_stage: AdoptionStage
    value_lever: str = Field(description="Main value delivered, max 10 words.")
    key_blocker: str = Field(description="Main thing holding it back, max 10 words.")
    evidence_ids: list[int]


class SegmentFindings(BaseModel):
    segment: Segment
    headline: str = Field(description="One sentence, max 25 words.")
    use_case_impact: list[UseCaseImpact] = Field(
        description="Every use case in this segment, ranked by impact (highest first)."
    )
    top_drivers: list[RankedFactor]
    top_inhibitors: list[RankedFactor]
    high_adoption_approaches: list[Approach]
    low_adoption_patterns: list[Approach]


class BiasInsight(BaseModel):
    stated_framing: str = Field(description="How reports phrase the issue.")
    underlying_issue: str = Field(description="What it most likely really is.")
    root_cause_id: str
    implication: str = Field(description="What an enterprise should do about it.")
    evidence_ids: list[int]


class RankedPoint(BaseModel):
    point: str = Field(description="Short label, max 10 words.")
    detail: str = Field(description="One sentence, max 20 words.")
    root_cause_id: str
    evidence_ids: list[int]


class AdvicePoint(BaseModel):
    action: str = Field(description="Imperative action, max 12 words.")
    rationale: str = Field(description="One sentence, max 20 words.")
    evidence_ids: list[int]


class IndustryProfile(BaseModel):
    industry_id: IndustryId
    headline: str = Field(description="One sentence, max 25 words.")
    evidence_strength: Literal["thin", "moderate", "strong"]
    top_use_case_ids: list[str] = Field(
        description="Up to 8 catalog use case ids most deployed in this industry, ranked."
    )
    drivers: list[RankedPoint] = Field(description="Exactly 5, strongest first.")
    blockers: list[RankedPoint] = Field(description="Exactly 5, strongest first.")
    advice: list[AdvicePoint] = Field(description="Exactly 5 actions to drive success.")


class SegmentSynthesis(BaseModel):
    """Output of the segment analyst."""

    segments: list[SegmentFindings]
    semantic_bias_insights: list[BiasInsight]
    source_bias_notes: list[str] = Field(
        description="Short observations on vendor sponsorship, sample quality, etc."
    )
    change_since_last_run: str = Field(
        description="Max 3 sentences on what is new versus the previous report."
    )
    open_questions: list[str]


class IndustrySynthesis(BaseModel):
    """Output of the industry analyst."""

    industries: list[IndustryProfile]


class Synthesis(SegmentSynthesis):
    """Merged brief that drives the report."""

    industries: list[IndustryProfile]
