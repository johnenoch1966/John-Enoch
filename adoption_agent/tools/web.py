"""Web retrieval tool: fetch a URL and return clean, bounded text.

Search grounding returns redirect links and snippets; this tool resolves the
final URL and extracts readable text (HTML or PDF) so researchers can quote
the primary source. Output is truncated to keep the context window small.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import re
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from .. import config

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (compatible; AdoptionResearchAgent/1.0; "
    "+https://github.com/johnenoch1966/John-Enoch)"
)
_cache: dict[str, dict] = {}


def _is_public_http_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
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
    if not _is_public_http_url(url):
        return {"status": "error", "error_message": f"Refusing non-public URL: {url}"}
    if url in _cache:
        return _cache[url]
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=config.FETCH_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)
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
        result = {
            "status": "ok",
            "final_url": str(resp.url),
            "title": title[:300],
            "text": text[:limit],
            "truncated": len(text) > limit,
        }
        _cache[url] = result
        return result
    except Exception as e:  # network errors are reported to the model, not raised
        logger.warning("fetch_page failed for %s: %s", url, e)
        return {"status": "error", "error_message": f"{type(e).__name__}: {e}"}
