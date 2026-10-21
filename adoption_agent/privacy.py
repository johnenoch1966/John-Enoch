"""PII detection and masking.

Defence in depth. PII is masked at every boundary where third-party content
enters the system or leaves it:

  1. `fetch_page` output is masked before it is returned to the model.
  2. `PiiRedactionPlugin` masks every text part of every model request and
     every tool result, for all agents (including nested search agents).
  3. `record_evidence` masks claims and source fields before they are persisted.
  4. Telemetry masks tool arguments and results before they are logged.

Backends
--------
* ``regex`` (default): fast, offline, deterministic, and covered by tests.
  It detects emails, phone numbers, payment cards (with a Luhn check),
  IBANs, US SSNs, UK National Insurance numbers, IPv4 addresses and
  credentials embedded in URLs.
* ``dlp``: Google Cloud Sensitive Data Protection (DLP) ``content:deidentify``,
  which also detects PERSON_NAME and many more info types. The regex pass
  always runs first, and if DLP is unavailable the regex result is kept, so
  masking never fails open.

Masked values are replaced with typed placeholders such as
``[REDACTED_EMAIL]`` so downstream analysis still knows *something* was
there without seeing the value.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

PLACEHOLDER = "[REDACTED_{}]"


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ip_ok(candidate: str) -> bool:
    return all(0 <= int(p) <= 255 for p in candidate.split("."))


def _phone_ok(candidate: str) -> bool:
    return 9 <= sum(c.isdigit() for c in candidate) <= 15


# (type, pattern, optional validator). Order matters: more specific first.
_RULES: list[tuple[str, re.Pattern[str], Callable[[str], bool] | None]] = [
    ("URL_CREDENTIALS", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), None),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"), None),
    ("CREDIT_CARD", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), _luhn_ok),
    ("US_SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), None),
    (
        "UK_NINO",
        re.compile(r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z]{2} ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b"),
        None,
    ),
    (
        "PHONE",
        re.compile(
            r"(?:\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4})"  # international
            r"|(?:\(\d{3}\)\s?\d{3}[\s.-]\d{4})"  # (555) 123-4567
            r"|(?:\b\d{3}[.-]\d{3}[.-]\d{4}\b)"  # 555-123-4567
        ),
        _phone_ok,
    ),
    ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), _ip_ok),
]

PII_TYPES = [name for name, _, _ in _RULES] + ["PERSON_NAME"]


def redact_regex(text: str) -> tuple[str, Counter[str]]:
    """Mask PII with the built-in rules. Returns (masked_text, counts_by_type)."""
    counts: Counter[str] = Counter()
    if not text:
        return text, counts
    for name, pattern, validator in _RULES:

        def repl(m: re.Match[str], name: str = name, validator=validator) -> str:
            if validator and not validator(m.group(0)):
                return m.group(0)
            counts[name] += 1
            return PLACEHOLDER.format(name)

        text = pattern.sub(repl, text)
    return text, counts


# ---------------------------------------------------------------------------
# Optional Cloud DLP backend
# ---------------------------------------------------------------------------
_DLP_INFO_TYPES = [
    "PERSON_NAME",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD_NUMBER",
    "IBAN_CODE",
    "US_SOCIAL_SECURITY_NUMBER",
    "UK_NATIONAL_INSURANCE_NUMBER",
    "IP_ADDRESS",
    "STREET_ADDRESS",
    "DATE_OF_BIRTH",
]


def redact_dlp(text: str, project: str) -> tuple[str, Counter[str]]:
    """Mask PII with Cloud DLP `content:deidentify` (raises on failure)."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)
    body = {
        "item": {"value": text},
        "inspectConfig": {"infoTypes": [{"name": t} for t in _DLP_INFO_TYPES]},
        "deidentifyConfig": {
            "infoTypeTransformations": {
                "transformations": [{"primitiveTransformation": {"replaceWithInfoTypeConfig": {}}}]
            }
        },
    }
    resp = session.post(
        f"https://dlp.googleapis.com/v2/projects/{project}/locations/global/content:deidentify",
        json=body,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    counts: Counter[str] = Counter()
    for t in data.get("overview", {}).get("transformationSummaries", []):
        counts[t.get("infoType", {}).get("name", "UNKNOWN")] += int(
            t.get("transformedBytes", 0) > 0
        )
    # DLP emits [INFO_TYPE]; normalise to our placeholder style.
    masked = re.sub(
        r"\[([A-Z_]+)\]", lambda m: PLACEHOLDER.format(m.group(1)), data["item"]["value"]
    )
    return masked, counts


def redact(text: str) -> tuple[str, Counter[str]]:
    """Mask PII using the configured backend. Never fails open."""
    masked, counts = redact_regex(text)
    if os.getenv("ADOPTION_AGENT_PII_BACKEND", "regex").lower() == "dlp" and masked:
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
        try:
            masked, dlp_counts = redact_dlp(masked, project)
            counts.update(dlp_counts)
        except Exception as e:  # keep regex-masked text
            logger.warning("DLP de-identification unavailable, using regex only: %s", e)
    return masked, counts


def mask(text: str) -> str:
    """Convenience wrapper returning only the masked text."""
    return redact(text)[0]


def redact_obj(obj: Any, counts: Counter[str] | None = None) -> tuple[Any, Counter[str]]:
    """Recursively mask all strings inside dicts/lists (e.g. tool results)."""
    counts = counts if counts is not None else Counter()
    if isinstance(obj, str):
        masked, c = redact_regex(obj)
        counts.update(c)
        return masked, counts
    if isinstance(obj, dict):
        return {k: redact_obj(v, counts)[0] for k, v in obj.items()}, counts
    if isinstance(obj, list):
        return [redact_obj(v, counts)[0] for v in obj], counts
    return obj, counts


def count_placeholders(text: str) -> Counter[str]:
    """Count masked items in already-redacted text (for reporting)."""
    return Counter(re.findall(r"\[REDACTED_([A-Z_]+)\]", text or ""))
