"""PII masking: detection rules, false-positive guards, and every enforcement point."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from google.genai import types

from adoption_agent import config
from adoption_agent.guardrails import PiiRedactionPlugin
from adoption_agent.privacy import count_placeholders, redact, redact_obj, redact_regex
from adoption_agent.store import EvidenceStore
from adoption_agent.tools import web
from adoption_agent.tools.evidence import record_evidence
from tests.samples import sample_claim, sample_source

# ------------------------------------------------------------------ detection
PII_CASES = [
    ("Contact jane.doe@example.com for details", "EMAIL", "jane.doe@example.com"),
    ("Call +44 20 7946 0958 today", "PHONE", "+44 20 7946 0958"),
    ("US office: (415) 555-0132", "PHONE", "(415) 555-0132"),
    ("Card 4111 1111 1111 1111 was charged", "CREDIT_CARD", "4111 1111 1111 1111"),
    ("Pay to GB82 WEST 1234 5698 7654 32", "IBAN", "GB82 WEST 1234 5698 7654 32"),
    ("SSN 123-45-6789 on file", "US_SSN", "123-45-6789"),
    ("NI number JG 10 37 48 C", "UK_NINO", "JG 10 37 48 C"),
    ("Request from 192.168.10.24 blocked", "IP_ADDRESS", "192.168.10.24"),
    ("https://admin:s3cret@intranet.example.com/x", "URL_CREDENTIALS", "admin:s3cret"),
]


@pytest.mark.parametrize("text,kind,secret", PII_CASES)
def test_detects_and_masks_pii(text, kind, secret):
    masked, counts = redact_regex(text)
    assert secret not in masked
    assert f"[REDACTED_{kind}]" in masked
    assert counts[kind] == 1


@pytest.mark.parametrize(
    "text",
    [
        "62% of 1,100 IT leaders cite governance (2024-2025 survey).",
        "Adoption rose from 12.5% to 31% between 2023 and 2025.",
        "Gartner predicts 40% of projects will be cancelled by 2027.",
        "Order 4111 1111 1111 1112 failed",  # fails the Luhn check -> not a card
        "Version 1.2.3 of the SDK; build 10.4.2.900 is not an IP",
        "$30 per user per month; 25-30 minutes saved per day",
    ],
)
def test_research_statistics_are_not_masked(text):
    """False-positive guard: numbers that matter to the analysis survive."""
    masked, counts = redact_regex(text)
    assert masked == text and not counts


def test_masking_is_idempotent_and_countable():
    once, _ = redact_regex("mail a@b.co and c@d.org")
    twice, counts = redact_regex(once)
    assert once == twice and not counts
    assert count_placeholders(once) == {"EMAIL": 2}


def test_redact_obj_masks_nested_tool_results():
    result = {"status": "ok", "items": [{"note": "ping bob@corp.com"}], "n": 3}
    masked, counts = redact_obj(result)
    assert masked["items"][0]["note"] == "ping [REDACTED_EMAIL]"
    assert masked["n"] == 3 and counts["EMAIL"] == 1


def test_dlp_backend_failure_never_fails_open(monkeypatch):
    monkeypatch.setenv("ADOPTION_AGENT_PII_BACKEND", "dlp")

    def boom(*a, **k):
        raise RuntimeError("DLP unavailable")

    monkeypatch.setattr("adoption_agent.privacy.redact_dlp", boom)
    masked, counts = redact("email x@y.com")
    assert masked == "email [REDACTED_EMAIL]" and counts["EMAIL"] == 1


# ------------------------------------------------------- enforcement points
def test_record_evidence_masks_before_storage(tmp_home, tool_context):
    claim = sample_claim("Contact cio@bank.com re: rollout")
    claim.evidence_quote = "Said the CIO (cio@bank.com, +1 415 555 0132)."
    res = record_evidence(sample_source(1), [claim], tool_context)
    assert res["status"] == "ok" and res["pii_masked"] >= 3

    stored = EvidenceStore(config.EVIDENCE_DB).all_claims()[0]
    blob = " ".join(str(v) for v in stored.values())
    assert "cio@bank.com" not in blob and "555 0132" not in blob
    assert "[REDACTED_EMAIL]" in stored["evidence_quote"]


def test_backfill_masks_previously_stored_pii(tmp_home):
    store = EvidenceStore(config.EVIDENCE_DB)
    sid, _ = store.add_source(sample_source(1), "r0")
    store.add_claim(sid, sample_claim("Email ops@legacy.com"), "r0")  # bypasses the tool
    assert store.apply_masking(redact_regex) == 2  # stated_factor + evidence_quote
    assert "ops@legacy.com" not in str(store.all_claims())
    assert store.apply_masking(redact_regex) == 0  # idempotent


async def test_fetch_page_masks_before_model_sees_content(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<article>Press: media@vendor.com, +44 20 7946 0958. 70% adopt.</article>",
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    web._cache.clear()
    res = await web.fetch_page("https://example.org/press")
    assert "media@vendor.com" not in res["text"] and "7946" not in res["text"]
    assert "70% adopt" in res["text"] and res["pii_masked"] == 2


async def test_plugin_masks_model_requests_and_tool_results(tmp_home):
    plugin = PiiRedactionPlugin()
    req = SimpleNamespace(
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(text="Reach me at ann@example.com"),
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="fetch_page", response={"text": "SSN 123-45-6789"}
                        )
                    ),
                ],
            )
        ]
    )
    ctx = SimpleNamespace(agent_name="researcher_0")
    assert await plugin.before_model_callback(callback_context=ctx, llm_request=req) is None
    parts = req.contents[0].parts
    assert parts[0].text == "Reach me at [REDACTED_EMAIL]"
    assert parts[1].function_response.response == {"text": "SSN [REDACTED_US_SSN]"}

    tool = SimpleNamespace(name="fetch_page")
    tctx = SimpleNamespace(agent_name="researcher_0")
    out = await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=tctx, result={"text": "ip 10.1.2.3"}
    )
    assert out == {"text": "ip [REDACTED_IP_ADDRESS]"}
    clean = await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=tctx, result={"text": "no pii here"}
    )
    assert clean is None  # untouched results pass through

    summary = plugin.summary()
    assert summary["totals"]["pii_items_masked"] == 3
    assert summary["by_agent"]["researcher_0"] == {"EMAIL": 1, "US_SSN": 1, "IP_ADDRESS": 1}

    run = SimpleNamespace(invocation_id="i", session=SimpleNamespace(state={"run_id": "r9"}))
    plugin._depth = 1
    await plugin.after_run_callback(invocation_context=run)
    assert (config.RUNS_DIR / "r9" / "privacy.json").exists()


def test_pii_plugin_is_registered_on_app():
    from adoption_agent.agent import app

    assert any(isinstance(p, PiiRedactionPlugin) for p in app.plugins)
