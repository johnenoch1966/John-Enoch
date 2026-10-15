from __future__ import annotations

import httpx
import pytest

from adoption_agent.tools import web


@pytest.fixture
def mock_http(monkeypatch):
    pages: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return pages.get(str(request.url), httpx.Response(404))

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", factory)
    web._cache.clear()
    return pages


async def test_fetch_page_extracts_article_text(mock_http):
    mock_http["https://example.org/r"] = httpx.Response(
        200,
        headers={"content-type": "text/html"},
        text="<html><head><title>Agentic AI Survey</title><script>x()</script></head>"
        "<body><nav>menu</nav><article><p>62% cite governance.</p></article></body></html>",
    )
    res = await web.fetch_page("https://example.org/r")
    assert res["status"] == "ok"
    assert res["title"] == "Agentic AI Survey"
    assert "62% cite governance." in res["text"]
    assert "menu" not in res["text"] and "x()" not in res["text"]


async def test_fetch_page_truncates(mock_http, monkeypatch):
    monkeypatch.setattr(web.config, "MAX_PAGE_CHARS", 50)
    mock_http["https://example.org/long"] = httpx.Response(
        200, headers={"content-type": "text/html"}, text="<p>" + "word " * 500 + "</p>"
    )
    res = await web.fetch_page("https://example.org/long")
    assert res["truncated"] and len(res["text"]) == 50


async def test_fetch_page_reports_http_errors(mock_http):
    res = await web.fetch_page("https://example.org/missing")
    assert res["status"] == "error" and "404" in res["error_message"]


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://localhost:8080/", "http://10.0.0.1/", "ftp://x.org"]
)
async def test_fetch_page_refuses_non_public_urls(url):
    res = await web.fetch_page(url)
    assert res["status"] == "error" and "non-public" in res["error_message"]
