"""Evidence tools: persist sources/claims, root-cause mappings, citation checks.

Every write tool validates its input (ADK validates the Pydantic argument
types before the call; we add semantic checks) and returns a structured
result the model can act on, including actionable error messages rather
than exceptions.
"""

from __future__ import annotations

from google.adk.tools import ToolContext

from .. import config
from ..privacy import redact
from ..schemas import ClaimInput, RootCauseMappingInput, SourceInput
from ..store import EvidenceStore
from .taxonomy import taxonomy_ids


def get_store() -> EvidenceStore:
    return EvidenceStore(config.EVIDENCE_DB)


def mask_evidence(
    source: SourceInput, claims: list[ClaimInput]
) -> tuple[SourceInput, list[ClaimInput], int]:
    """Return copies of source/claims with PII masked, plus the number masked."""
    total = 0

    def m(text: str) -> str:
        nonlocal total
        masked, counts = redact(text)
        total += sum(counts.values())
        return masked

    src = source.model_copy(
        update={
            "url": m(source.url),
            "title": m(source.title),
            "methodology": m(source.methodology),
        }
    )
    masked_claims = [
        c.model_copy(
            update={
                "stated_factor": m(c.stated_factor),
                "evidence_quote": m(c.evidence_quote),
                "statistic": m(c.statistic),
            }
        )
        for c in claims
    ]
    return src, masked_claims, total


def _run_id(tool_context: ToolContext | None) -> str:
    if tool_context is None:
        return "adhoc"
    return str(tool_context.state.get("run_id", "adhoc"))


def record_evidence(
    source: SourceInput, claims: list[ClaimInput], tool_context: ToolContext
) -> dict:
    """Saves one public source and the adoption factors it reports.

    Call once per source after reading it with fetch_page. Each claim must be
    a single driver OR inhibitor of enterprise agentic-AI adoption, phrased
    the way the source phrases it, with a supporting quote.

    Args:
      source: Bibliographic details of the report/article (use the final URL).
      claims: The adoption drivers/inhibitors the source reports (1-10).

    Returns:
      dict with status, source_id, whether the source was new, the ids of
      saved claims, and counts of new vs duplicate claims.
    """
    if not claims:
        return {"status": "error", "error_message": "Provide at least one claim."}
    if not source.url.startswith(("http://", "https://")):
        return {"status": "error", "error_message": "source.url must be an http(s) URL."}
    # Privacy: mask PII before anything is persisted (storage-layer guardrail).
    source, claims, pii_masked = mask_evidence(source, claims)
    store = get_store()
    run_id = _run_id(tool_context)
    source_id, source_new = store.add_source(source, run_id)
    ids, new = [], 0
    for cl in claims[:10]:
        if not cl.evidence_quote.strip():
            continue
        cid, is_new = store.add_claim(source_id, cl, run_id)
        ids.append(cid)
        new += int(is_new)

    # Per-run counter in session state (short-term memory / progress tracking).
    agent = tool_context.agent_name if tool_context else "unknown"
    key = f"temp:sources_by_{agent}"
    if tool_context is not None:
        tool_context.state[key] = int(tool_context.state.get(key, 0)) + int(source_new)
    return {
        "status": "ok",
        "source_id": source_id,
        "source_is_new": source_new,
        "claim_ids": ids,
        "new_claims": new,
        "duplicate_claims": len(ids) - new,
        "pii_masked": pii_masked,
        "sources_recorded_by_you_this_run": (
            tool_context.state.get(key) if tool_context is not None else None
        ),
    }


def get_unmapped_claims(tool_context: ToolContext) -> dict:
    """Returns the next batch of claims that still need root-cause mapping.

    Returns:
      dict with 'remaining' (total unmapped count) and 'claims' (a batch of
      {id, stated_factor, polarity, segment, evidence_quote, publisher,
      source_type, vendor_affiliated}).
    """
    store = get_store()
    return {
        "remaining": store.count_unmapped(),
        "claims": store.unmapped_claims(limit=config.MAPPING_BATCH_SIZE),
    }


def record_root_cause_mappings(
    mappings: list[RootCauseMappingInput], tool_context: ToolContext
) -> dict:
    """Saves your root-cause (semantic bias) analysis for a batch of claims.

    Args:
      mappings: One entry per claim id, mapping the stated factor to one or
        more taxonomy category ids with your reasoning and any framing bias.

    Returns:
      dict with counts saved, any rejected entries with reasons, and how many
      claims remain unmapped.
    """
    store = get_store()
    valid_ids = taxonomy_ids()
    claim_ids = store.existing_claim_ids([m.claim_id for m in mappings])
    saved, rejected = 0, []
    for m in mappings:
        bad = [r for r in m.root_cause_ids if r not in valid_ids]
        if m.claim_id not in claim_ids:
            rejected.append({"claim_id": m.claim_id, "reason": "unknown claim_id"})
        elif not m.root_cause_ids or bad:
            rejected.append(
                {
                    "claim_id": m.claim_id,
                    "reason": f"invalid root_cause_ids {bad}; valid: {sorted(valid_ids)}",
                }
            )
        else:
            store.add_mapping(m, _run_id(tool_context))
            saved += 1
    return {
        "status": "ok" if not rejected else "partial",
        "saved": saved,
        "rejected": rejected,
        "remaining": store.count_unmapped(),
    }


def validate_citations(evidence_ids: list[int]) -> dict:
    """Checks that cited claim ids exist in the evidence store.

    Args:
      evidence_ids: All claim ids cited anywhere in the draft synthesis.

    Returns:
      dict with 'valid' (bool), 'missing' (ids that do not exist) and 'checked'.
    """
    ids = sorted(set(int(i) for i in evidence_ids))
    existing = get_store().existing_claim_ids(ids)
    missing = [i for i in ids if i not in existing]
    return {"valid": not missing, "missing": missing, "checked": len(ids)}
