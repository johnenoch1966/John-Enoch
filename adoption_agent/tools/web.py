"""Web retrieval tool: fetch a URL and return clean, bounded text.

Search grounding returns redirect links and snippets; this tool resolves the
final URL and extracts readable text (HTML or PDF) so researchers can quote
the primary source. Output is truncated to keep the context window small.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .. import config
from ..privacy import mask, redact

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (compatible; AdoptionResearchAgent/1.0; "
    "+https://github.com/johnenoch1966/John-Enoch)"
)
_cache: dict[str, dict] = {}
_MAX_REDIRECTS = 5


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _is_public_http_url(url: str) -> bool:
    """Syntactic check: http(s) scheme, a host, and no literal non-public IP."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        return _ip_is_public(ipaddress.ip_address(host))
    except ValueError:
        return True


async def _resolve_host(host: str) -> list[str]:
    """IP addresses a hostname resolves to (empty if it does not resolve)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return []
    return [info[4][0] for info in infos]


async def _is_safe_target(url: str) -> bool:
    """Public URL whose hostname does not resolve to a non-public address.

    An unresolvable host is allowed here: the HTTP client cannot connect to it
    either, so the request fails safely on its own.
    """
    if not _is_public_http_url(url):
        return False
    for addr in await _resolve_host(urlsplit(url).hostname or ""):
        try:
            if not _ip_is_public(ipaddress.ip_address(addr.split("%")[0])):
                return False
        except ValueError:
            return False
    return True


def _html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text(separator="\n")
    return title, text


def _pdf_to_text(data: bytes) -> tuple[str, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    title = (reader.metadata.title if reader.metadata else "") or ""
    pages = []
    for page in reader.pages[:25]:
        pages.append(page.extract_text() or "")
        if sum(len(p) for p in pages) > config.MAX_PAGE_CHARS * 2:
            break
    return title, "\n".join(pages)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


async def fetch_page(url: str) -> dict:
    """Fetches a public web page or PDF and returns its readable text.

    Use this after a search to read the primary source of a report so you can
    extract an accurate quote, publisher, publication date and methodology.

    Args:
      url: Absolute http(s) URL (search redirect links are followed).

    Returns:
      dict with keys: status ('ok' | 'error'), final_url, title, text
      (truncated to a few thousand characters), truncated (bool), and
      error_message on failure.
    """
    if not await _is_safe_target(url):
        return {"status": "error", "error_message": f"Refusing non-public URL: {url}"}
    if url in _cache:
        return _cache[url]
    try:
        # Redirects are followed manually so every hop is checked; otherwise a
        # public URL could redirect the fetcher to an internal address.
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=config.FETCH_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            current = url
            for _ in range(_MAX_REDIRECTS + 1):
                resp = await client.get(current)
                location = resp.headers.get("location")
                if not (resp.is_redirect and location):
                    break
                current = str(resp.url.join(location))
                if not await _is_safe_target(current):
                    return {
                        "status": "error",
                        "error_message": f"Refusing redirect to non-public URL: {current}",
                    }
            else:
                return {"status": "error", "error_message": "Too many redirects"}
        if resp.status_code >= 400:
            return {
                "status": "error",
                "final_url": str(resp.url),
                "error_message": f"HTTP {resp.status_code}",
            }
        ctype = resp.headers.get("content-type", "").lower()
        if "pdf" in ctype or str(resp.url).lower().endswith(".pdf"):
            title, text = _pdf_to_text(resp.content)
        else:
            title, text = _html_to_text(resp.text)
        text = _clean(text)
        limit = config.MAX_PAGE_CHARS
        # Mask PII before third-party content can enter the model context.
        masked_text, pii = redact(text[:limit])
        result = {
            "status": "ok",
            "final_url": str(resp.url),
            "title": mask(title[:300]),
            "text": masked_text,
            "truncated": len(text) > limit,
            "pii_masked": int(sum(pii.values())),
        }
        _cache[url] = result
        return result
    except Exception as e:  # network errors are reported to the model, not raised
        logger.warning("fetch_page failed for %s: %s", url, e)
        return {"status": "error", "error_message": f"{type(e).__name__}: {e}"}
