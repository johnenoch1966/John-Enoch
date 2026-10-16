"""Shared sample data for tests."""

from __future__ import annotations

from adoption_agent import config
from adoption_agent.schemas import (
    AdvicePoint,
    Approach,
    BiasInsight,
    ClaimInput,
    IndustryProfile,
    IndustrySynthesis,
    RankedFactor,
    RankedPoint,
    SegmentFindings,
    SegmentSynthesis,
    SourceInput,
    Synthesis,
    UseCase,
    UseCaseCatalog,
    UseCaseCluster,
    UseCaseImpact,
)

INDUSTRY_CYCLE = ["fsi", "hcls", "manufacturing", "retail_cpg", "tmt"]


def sample_source(n: int = 1, vendor: bool = False) -> SourceInput:
    return SourceInput(
        url=f"https://example.org/report-{n}?utm_source=x",
        title=f"Report {n}",
        publisher="Example Research",
        published="2025-06",
        source_type="vendor" if vendor else "analyst",
        vendor_affiliated=vendor,
        methodology="survey of 1,000 IT leaders",
    )


def sample_claim(
    factor: str = "Connectors not enabled",
    polarity: str = "inhibitor",
    segment: str = "copilot",
    industries: list[str] | None = None,
    use_case_ids: list[str] | None = None,
) -> ClaimInput:
    return ClaimInput(
        stated_factor=factor,
        polarity=polarity,
        segment=segment,
        use_case_ids=use_case_ids if use_case_ids is not None else ["uc-0"],
        evidence_quote=f"Many firms report: {factor}.",
        statistic="41% of respondents",
        industries=industries or [],
    )


def sample_catalog(n_use_cases: int | None = None) -> UseCaseCatalog:
    n = n_use_cases or config.TARGET_USE_CASES
    names = ["copilot", "customer-service", "fs-ops", "it-eng", "back-office"]
    use_cases, clusters = [], []
    per = n // len(names)
    idx = 0
    for ci, cname in enumerate(names):
        seg = "copilot" if ci == 0 else "workflow"
        count = per if ci < len(names) - 1 else n - per * (len(names) - 1)
        ids = []
        for _ in range(count):
            uid = f"uc-{idx}"
            use_cases.append(
                UseCase(
                    id=uid,
                    name=f"Use case {idx}",
                    segment=seg,
                    domain=cname,
                    description="desc",
                    example_industries=["Banking"],
                    industries=[INDUSTRY_CYCLE[idx % len(INDUSTRY_CYCLE)]],
                    prevalence_evidence="common",
                )
            )
            ids.append(uid)
            idx += 1
        clusters.append(UseCaseCluster(id=cname, name=cname.title(), segment=seg, use_case_ids=ids))
    return UseCaseCatalog(
        generated_at="2026-01-01T00:00:00+00:00", use_cases=use_cases, clusters=clusters
    )


def sample_segment_synthesis(
    ids: list[int], driver: str = "Executive sponsorship"
) -> SegmentSynthesis:
    def seg(name: str) -> SegmentFindings:
        first = 0 if name == "copilot" else 10
        return SegmentFindings(
            segment=name,
            headline=f"{name} headline",
            use_case_impact=[
                UseCaseImpact(
                    use_case_id=f"uc-{first}",
                    impact_score=3,
                    adoption_stage="piloting",
                    value_lever="Faster drafting",
                    key_blocker="Data oversharing",
                    evidence_ids=ids,
                ),
                UseCaseImpact(
                    use_case_id=f"uc-{first + 1}",
                    impact_score=5,
                    adoption_stage="scaling",
                    value_lever="Cycle time cut",
                    key_blocker="Audit trail gaps",
                    evidence_ids=ids,
                ),
            ],
            top_drivers=[
                RankedFactor(
                    factor=driver,
                    root_cause_id="operating_model",
                    explanation="x",
                    strength="strong",
                    evidence_ids=ids,
                )
            ],
            top_inhibitors=[
                RankedFactor(
                    factor="Connectors disabled",
                    root_cause_id="risk_auditability",
                    explanation="y",
                    strength="moderate",
                    evidence_ids=ids,
                )
            ],
            high_adoption_approaches=[
                Approach(approach="Redesign workflows", rationale="r", evidence_ids=ids)
            ],
            low_adoption_patterns=[
                Approach(approach="Seat-only rollout", rationale="r", evidence_ids=ids)
            ],
        )

    return SegmentSynthesis(
        segments=[seg("copilot"), seg("workflow")],
        semantic_bias_insights=[
            BiasInsight(
                stated_framing="connectors not enabled",
                underlying_issue="no audit trail",
                root_cause_id="risk_auditability",
                implication="build controls",
                evidence_ids=ids,
            )
        ],
        source_bias_notes=["Vendor surveys dominate."],
        change_since_last_run="First run.",
        open_questions=["How is ROI measured?"],
    )


def sample_industry_synthesis(ids: list[int]) -> IndustrySynthesis:
    def pts(prefix: str) -> list[RankedPoint]:
        return [
            RankedPoint(
                point=f"{prefix} {i}",
                detail="d",
                root_cause_id="regulatory_compliance",
                evidence_ids=ids,
            )
            for i in range(1, 6)
        ]

    return IndustrySynthesis(
        industries=[
            IndustryProfile(
                industry_id="fsi",
                headline="FSI headline",
                evidence_strength="strong",
                top_use_case_ids=["uc-10", "uc-0", "not-a-use-case"],
                drivers=pts("FSI driver"),
                blockers=pts("FSI blocker"),
                advice=[
                    AdvicePoint(action=f"FSI action {i}", rationale="r", evidence_ids=ids)
                    for i in range(1, 6)
                ],
            )
        ]
    )


def sample_synthesis(ids: list[int], driver: str = "Executive sponsorship") -> Synthesis:
    seg = sample_segment_synthesis(ids, driver)
    return Synthesis(**seg.model_dump(), industries=sample_industry_synthesis(ids).industries)
