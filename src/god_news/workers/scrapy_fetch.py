from __future__ import annotations

import json

# mypy: disable-error-code=misc
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import CloseSpider, IgnoreRequest
from scrapy.http import Request, Response, TextResponse
from scrapy.linkextractors import LinkExtractor
from trafilatura import bare_extraction

from god_news.infrastructure.fetchers.html_content import (
    choose_article_text,
    extract_html_content,
)
from god_news.infrastructure.fetchers.scrapy import ScrapyWorkerRequest, ScrapyWorkerResponse
from god_news.infrastructure.fetchers.url_policy import UrlPolicy, normalize_allowed_ports


class UrlSafetyDownloaderMiddleware:
    """Revalidates every Scrapy request, including redirects and robots.txt."""

    def __init__(self, allow_private: bool, allowed_ports: tuple[int, ...]) -> None:
        self._policy = UrlPolicy(allow_private=allow_private, allowed_ports=allowed_ports)

    @classmethod
    def from_crawler(cls, crawler: Any) -> UrlSafetyDownloaderMiddleware:
        return cls(
            allow_private=crawler.settings.getbool("GOD_NEWS_ALLOW_PRIVATE", False),
            allowed_ports=normalize_allowed_ports(
                crawler.settings.getlist("GOD_NEWS_ALLOWED_PORTS")
            ),
        )

    def process_request(self, request: Request) -> None:
        try:
            self._policy.validate_sync(request.url)
        except Exception as exc:
            raise IgnoreRequest("URL rejected by god-news network policy") from exc


@dataclass(slots=True)
class _ResultHolder:
    response: ScrapyWorkerResponse | None = None
    root_outbound_links: list[str] | None = None


def _json_ld_article_metadata(response: TextResponse) -> tuple[str | None, datetime | None]:
    discovered_headline: str | None = None
    for raw in response.css('script[type="application/ld+json"]::text').getall():
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            headline = candidate.get("headline")
            if isinstance(headline, str) and not discovered_headline:
                discovered_headline = headline
            published = candidate.get("datePublished")
            try:
                published_at = (
                    datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if isinstance(published, str)
                    else None
                )
            except ValueError:
                published_at = None
            if published_at is not None:
                return (
                    headline if isinstance(headline, str) else discovered_headline,
                    published_at,
                )
    time_value = response.css("time[datetime]::attr(datetime)").get()
    if time_value:
        try:
            return discovered_headline, datetime.fromisoformat(time_value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return discovered_headline, None


def _run(request: ScrapyWorkerRequest) -> ScrapyWorkerResponse:
    holder = _ResultHolder()
    parsed = urlsplit(request.url)
    parsed_hostname = parsed.hostname
    if parsed_hostname is None:
        return ScrapyWorkerResponse(ok=False, error="Scrapy source has no hostname.")
    start_netloc = parsed.netloc

    class BoundedSiteSpider(scrapy.Spider):
        name = "god_news_bounded_site"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._links = LinkExtractor(allow_domains=(start_netloc,), unique=True)

        async def start(self):  # type: ignore[no-untyped-def]
            yield scrapy.Request(
                request.url,
                callback=self.parse,
                errback=self.on_error,
                dont_filter=True,
            )

        def parse(self, response: Response, **kwargs: Any):  # type: ignore[no-untyped-def]
            del kwargs
            if not isinstance(response, TextResponse):
                if holder.response is None:
                    holder.response = ScrapyWorkerResponse(
                        ok=False,
                        final_url=response.url,
                        error="Scrapy source was not a text document.",
                    )
                return
            # Scrapy's detector can misclassify UTF-8 Chinese pages that contain
            # mostly CJK text. Prefer UTF-8 only when the bytes decode strictly;
            # otherwise retain the response's declared/detected encoding.
            try:
                response.body.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                replacement = response.replace(body=response.body, encoding="utf-8")
                if isinstance(replacement, TextResponse):
                    response = replacement
            outbound_links = [link.url for link in self._links.extract_links(response)][:500]
            is_requested_page = response.url.rstrip("/") == request.url.rstrip("/")
            if is_requested_page and outbound_links:
                holder.root_outbound_links = outbound_links
            structured_title, structured_published_at = _json_ld_article_metadata(response)
            visible = extract_html_content(response.text, response.url)
            document = bare_extraction(
                response.body,
                url=response.url,
                with_metadata=True,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                deduplicate=True,
                fast=False,
            )
            if isinstance(document, dict):
                text = document.get("text")
                title = document.get("title")
                author = document.get("author")
                published_at = document.get("date")
            elif document is not None:
                text = document.text
                title = document.title
                author = document.author
                published_at = document.date
            else:
                text = title = author = published_at = None
            cleaned = choose_article_text(
                text if isinstance(text, str) else None,
                visible.text,
                request.min_content_characters,
            )
            if not cleaned and visible.video_links:
                fallback_title = structured_title or title or response.css("title::text").get()
                cleaned = fallback_title.strip() if isinstance(fallback_title, str) else ""
            if cleaned:
                candidate = ScrapyWorkerResponse(
                    ok=True,
                    final_url=response.url,
                    title=(
                        structured_title
                        or (str(title) if title else response.css("title::text").get())
                    ),
                    content=cleaned,
                    author=str(author) if author else None,
                    published_at=structured_published_at or published_at,
                    outbound_links=holder.root_outbound_links or outbound_links,
                    video_links=visible.video_links,
                    http_status=response.status,
                )
                previous_size = (
                    len(holder.response.content)
                    if holder.response is not None and holder.response.content is not None
                    else 0
                )
                if len(cleaned) > previous_size:
                    holder.response = candidate
                if len(cleaned) >= request.min_content_characters or visible.video_links:
                    raise CloseSpider("article_content_found")
            elif holder.response is None:
                holder.response = ScrapyWorkerResponse(
                    ok=False,
                    final_url=response.url,
                    error="Trafilatura could not extract a document.",
                )

            for link in outbound_links:
                yield scrapy.Request(
                    link,
                    callback=self.parse,
                    errback=self.on_error,
                )

        def on_error(self, failure: Any) -> None:
            sys.stderr.write(f"Scrapy request error: {type(failure.value).__name__}\n")
            if holder.response is None:
                response = getattr(failure.value, "response", None)
                status = getattr(response, "status", None)
                access_challenge = status == 403
                holder.response = ScrapyWorkerResponse(
                    ok=False,
                    final_url=getattr(response, "url", None),
                    http_status=status,
                    error_code=(
                        "access_challenge_detected" if access_challenge else "fetch_failed"
                    ),
                    error=(
                        "The source refused automated access with HTTP 403."
                        if access_challenge
                        else "Scrapy request failed."
                    ),
                )

    process = CrawlerProcess(
        settings={
            "ROBOTSTXT_OBEY": True,
            "COOKIES_ENABLED": False,
            "DOWNLOAD_TIMEOUT": request.download_timeout_seconds,
            "DOWNLOAD_MAXSIZE": request.max_response_bytes,
            "REDIRECT_MAX_TIMES": request.redirect_max_times,
            "RETRY_TIMES": request.retry_times,
            "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
            "CONCURRENT_REQUESTS": 1,
            "DEPTH_LIMIT": request.depth_limit,
            "CLOSESPIDER_PAGECOUNT": request.close_page_count,
            "CLOSESPIDER_TIMEOUT": max(1, request.download_timeout_seconds * 2),
            "AUTOTHROTTLE_ENABLED": True,
            "USER_AGENT": request.user_agent,
            "GOD_NEWS_ALLOW_PRIVATE": request.allow_private,
            "GOD_NEWS_ALLOWED_PORTS": [str(port) for port in request.allowed_ports],
            "DOWNLOADER_MIDDLEWARES": {
                "god_news.workers.scrapy_fetch.UrlSafetyDownloaderMiddleware": 50,
            },
            "LOG_LEVEL": "WARNING",
        }
    )
    process.crawl(BoundedSiteSpider)
    process.start(stop_after_crawl=True, install_signal_handlers=False)
    return holder.response or ScrapyWorkerResponse(
        ok=False,
        error="Scrapy finished without producing a response.",
    )


def main() -> int:
    try:
        request = ScrapyWorkerRequest.model_validate_json(sys.stdin.buffer.read())
        result = _run(request)
    except Exception as exc:
        sys.stderr.write(f"Scrapy worker internal error: {type(exc).__name__}\n")
        result = ScrapyWorkerResponse(ok=False, error="Scrapy worker failed.")
    sys.stdout.write(result.model_dump_json())
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
