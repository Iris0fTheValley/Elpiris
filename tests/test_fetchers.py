from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

from god_news.demo.adapters import DeterministicFetcher
from god_news.domain.models import FetchedDocument, SourceRequest, TextSource, UrlSource
from god_news.errors import FetchError, FetchPolicyError
from god_news.infrastructure.fetchers.chain import FetcherChain
from god_news.infrastructure.fetchers.drission import (
    DrissionPageFetcher,
    DrissionWorkerResponse,
)
from god_news.infrastructure.fetchers.jina import JinaReaderFetcher
from god_news.infrastructure.fetchers.scrapy import (
    ScrapyTrafilaturaFetcher,
    ScrapyWorkerRequest,
    ScrapyWorkerResponse,
)
from god_news.infrastructure.fetchers.url_policy import UrlPolicy, normalize_allowed_ports
from god_news.infrastructure.processes import run_json_worker


class FailingFetcher:
    def __init__(self, name: str, *, retryable: bool = True) -> None:
        self._name = name
        self._retryable = retryable
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def fetch(self, source: SourceRequest) -> FetchedDocument:
        del source
        self.calls += 1
        raise FetchError(f"{self.name} failed", retryable=self._retryable)

    async def aclose(self) -> None:
        return None


class SuccessfulFetcher:
    name = "success"

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, source: SourceRequest) -> FetchedDocument:
        del source
        self.calls += 1
        return FetchedDocument.from_text(
            TextSource(title="Fallback", text="Recovered article body.")
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_fetcher_chain_falls_back_in_order() -> None:
    first = FailingFetcher("first", retryable=False)
    second = FailingFetcher("second")
    success = SuccessfulFetcher()
    chain = FetcherChain([first, second, success])
    traced = await chain.fetch_with_trace(UrlSource(url="https://8.8.8.8/article"))
    result = traced.document
    assert result.content == "Recovered article body."
    assert [first.calls, second.calls, success.calls] == [1, 1, 1]
    assert [attempt.layer for attempt in traced.attempts] == ["first", "second", "success"]
    assert [attempt.outcome for attempt in traced.attempts] == [
        "failed",
        "failed",
        "succeeded",
    ]
    assert all(attempt.duration_ms >= 0 for attempt in traced.attempts)
    assert traced.attempts[0].retryable is False

    text_result = await chain.fetch(TextSource(text="Direct uploaded text", title="Direct"))
    assert text_result.source.fetcher == "text"


@pytest.mark.asyncio
async def test_deterministic_fetcher_supports_url_without_network() -> None:
    fetcher = DeterministicFetcher()
    document = await fetcher.fetch(UrlSource(url="https://example.com/offline-fixture"))
    assert document.source.fetcher == "deterministic-offline"
    assert document.source.final_uri == "https://example.com/offline-fixture"
    assert "lost dog" in document.content


def test_scrapy_port_settings_are_normalized_to_integers() -> None:
    assert normalize_allowed_ports(["80", "443"]) == (80, 443)


@pytest.mark.asyncio
async def test_url_policy_rejects_private_addresses() -> None:
    policy = UrlPolicy()
    with pytest.raises(FetchPolicyError):
        await policy.validate("http://127.0.0.1/admin")
    assert await policy.validate("https://8.8.8.8/article") == "https://8.8.8.8/article"


@pytest.mark.asyncio
async def test_jina_reader_validates_typed_json_contract() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        payload = {
            "code": 200,
            "data": {
                "content": "A" * 250 + " [source video](https://media.example/clip.mp4)",
                "title": "Typed article",
                "url": "https://8.8.8.8/article",
                "httpStatus": 200,
                "publishedTime": "Wed, 01 Jul 2026 17:50:18 GMT",
            },
        }
        return httpx.Response(200, content=json.dumps(payload).encode(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = JinaReaderFetcher(
            client=client,
            policy=UrlPolicy(),
            base_url="https://r.jina.ai",
            api_key=None,
            page_timeout_seconds=10,
            max_response_bytes=10_000,
            min_content_characters=200,
        )
        result = await fetcher.fetch(UrlSource(url="https://8.8.8.8/article"))
    assert result.source.fetcher == "jina-reader"
    assert result.source.published_at is not None
    assert [str(link) for link in result.video_links] == ["https://media.example/clip.mp4"]
    assert captured[0].headers["accept"] == "application/json"
    assert "authorization" not in captured[0].headers


@pytest.mark.asyncio
async def test_jina_fragment_uses_post_and_enforces_stream_limit() -> None:
    requests: list[httpx.Request] = []

    async def success_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = {
            "code": 200,
            "data": {
                "content": "B" * 250,
                "title": "SPA article",
                "url": "https://8.8.8.8/#article",
                "httpStatus": 200,
            },
        }
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(success_handler)) as client:
        fetcher = JinaReaderFetcher(
            client=client,
            policy=UrlPolicy(),
            base_url="https://r.jina.ai",
            api_key=None,
            page_timeout_seconds=10,
            max_response_bytes=10_000,
            min_content_characters=200,
        )
        await fetcher.fetch(UrlSource(url="https://8.8.8.8/#article"))
    assert requests[0].method == "POST"
    assert b"url=https%3A%2F%2F8.8.8.8%2F%23article" in requests[0].content

    async def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 101, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(oversized_handler)) as client:
        fetcher = JinaReaderFetcher(
            client=client,
            policy=UrlPolicy(),
            base_url="https://r.jina.ai",
            api_key=None,
            page_timeout_seconds=10,
            max_response_bytes=100,
            min_content_characters=1,
        )
        with pytest.raises(FetchError, match="size limit"):
            await fetcher.fetch(UrlSource(url="https://8.8.8.8/article"))


@pytest.mark.asyncio
async def test_isolated_browser_and_scrapy_parent_adapters(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_worker(**kwargs):  # type: ignore[no-untyped-def]
        response_type = kwargs["response_type"]
        if response_type is DrissionWorkerResponse:
            return DrissionWorkerResponse(
                ok=True,
                final_url="https://8.8.8.8/article",
                title="Rendered",
                html=(
                    '<html><head><meta property="article:published_time" '
                    'content="2026-07-12T08:00:00Z"></head><body>'
                    '<a href="/next">Next</a>'
                    '<article><video><source src="/clip.mp4"></video>'
                    + "rendered " * 40
                    + "</article></body></html>"
                ),
            )
        return ScrapyWorkerResponse(
            ok=True,
            final_url="https://8.8.8.8/article",
            title="   ",
            content="extracted " * 40,
            outbound_links=["https://8.8.8.8/next"],
            video_links=["https://8.8.8.8/clip.mp4"],
        )

    monkeypatch.setattr(
        "god_news.infrastructure.fetchers.drission.run_json_worker",
        fake_worker,
    )
    monkeypatch.setattr(
        "god_news.infrastructure.fetchers.scrapy.run_json_worker",
        fake_worker,
    )
    policy = UrlPolicy()
    source = UrlSource(url="https://8.8.8.8/article")
    browser = DrissionPageFetcher(
        policy=policy,
        timeout_seconds=5,
        base_timeout_seconds=2,
        script_timeout_seconds=2,
        quit_timeout_seconds=2,
        max_concurrency=1,
        worker_module="unused",
        max_response_bytes=10_000,
        min_content_characters=20,
    )
    browser_result = await browser.fetch(source)
    assert browser_result.source.fetcher == "drission-page"
    assert browser_result.source.published_at is not None
    assert [str(link) for link in browser_result.outbound_links] == ["https://8.8.8.8/next"]
    assert [str(link) for link in browser_result.video_links] == ["https://8.8.8.8/clip.mp4"]

    scrapy = ScrapyTrafilaturaFetcher(
        policy=policy,
        timeout_seconds=5,
        worker_module="unused",
        max_response_bytes=10_000,
        min_content_characters=20,
        download_timeout_seconds=2,
        redirect_max_times=2,
        retry_times=0,
        depth_limit=1,
        close_page_count=1,
        user_agent="test",
    )
    scrapy_result = await scrapy.fetch(source)
    assert scrapy_result.source.fetcher == "scrapy-trafilatura"
    assert scrapy_result.source.title == "Untitled source"
    assert [str(link) for link in scrapy_result.outbound_links] == ["https://8.8.8.8/next"]
    assert [str(link) for link in scrapy_result.video_links] == ["https://8.8.8.8/clip.mp4"]


@pytest.mark.asyncio
async def test_browser_accepts_titled_video_page_without_article_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_worker(**kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return DrissionWorkerResponse(
            ok=True,
            final_url="https://8.8.8.8/video",
            title="Source clip",
            html='<html><body><article><video src="/clip.mp4"></video></article></body></html>',
        )

    monkeypatch.setattr("god_news.infrastructure.fetchers.drission.run_json_worker", fake_worker)
    fetcher = DrissionPageFetcher(
        policy=UrlPolicy(),
        timeout_seconds=5,
        base_timeout_seconds=2,
        script_timeout_seconds=2,
        quit_timeout_seconds=2,
        max_concurrency=1,
        worker_module="unused",
        max_response_bytes=10_000,
        min_content_characters=200,
    )

    result = await fetcher.fetch(UrlSource(url="https://8.8.8.8/video"))

    assert result.content == "Source clip"
    assert [str(link) for link in result.video_links] == ["https://8.8.8.8/clip.mp4"]


@pytest.mark.asyncio
async def test_scrapy_worker_conditionally_crawls_same_site_offline() -> None:
    article = "A verified local article sentence with useful facts. " * 40

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_head = await reader.readuntil(b"\r\n\r\n")
            path = request_head.split(b" ", 2)[1].split(b"?", 1)[0]
            if path == b"/robots.txt":
                content_type = "text/plain"
                body = b"User-agent: *\nAllow: /\n"
            elif path == b"/article":
                content_type = "text/html; charset=utf-8"
                body = (
                    "<html><head><title>Deep fixture</title>"
                    '<script type="application/ld+json">'
                    '{"headline":"Structured fixture",'
                    '"datePublished":"2026-07-12T08:00:00+08:00"}'
                    "</script></head><body>"
                    f"<article><h1>Deep fixture</h1><p>{article}</p>"
                    '<video><source src="/clip.mp4"></video></article>'
                    "</body></html>"
                ).encode()
            else:
                content_type = "text/html; charset=utf-8"
                body = b'<html><body><nav><a href="/article">Read article</a></nav></body></html>'
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        port = int(server.sockets[0].getsockname()[1])
        response = await run_json_worker(
            command=(sys.executable, "-m", "god_news.workers.scrapy_fetch"),
            request=ScrapyWorkerRequest(
                url=f"http://127.0.0.1:{port}/",
                allow_private=True,
                allowed_ports=(port,),
                max_response_bytes=100_000,
                min_content_characters=200,
                download_timeout_seconds=5,
                redirect_max_times=1,
                retry_times=0,
                depth_limit=2,
                close_page_count=5,
                user_agent="god-news-offline-test",
            ),
            response_type=ScrapyWorkerResponse,
            timeout_seconds=30,
        )
    finally:
        server.close()
        await server.wait_closed()
    assert response.ok
    assert response.final_url == f"http://127.0.0.1:{port}/article", response.model_dump()
    assert response.content is not None and "verified local article" in response.content
    assert response.title == "Structured fixture"
    assert response.published_at is not None
    assert response.published_at.utcoffset() is not None
    assert response.video_links == [f"http://127.0.0.1:{port}/clip.mp4"]
