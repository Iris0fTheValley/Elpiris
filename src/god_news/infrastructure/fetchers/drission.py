from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict

from god_news.domain.enums import SourceKind
from god_news.domain.models import FetchedDocument, SourceRequest, SourceSnapshot, UrlSource
from god_news.errors import FetchError
from god_news.infrastructure.fetchers.html_content import (
    choose_article_text,
    extract_html_content,
)
from god_news.infrastructure.fetchers.url_policy import UrlPolicy
from god_news.infrastructure.processes import run_json_worker


class DrissionWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    timeout_seconds: float
    base_timeout_seconds: float
    script_timeout_seconds: float
    quit_timeout_seconds: float
    max_response_bytes: int
    allow_private: bool
    allowed_ports: tuple[int, ...]


class DrissionWorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    final_url: str | None = None
    title: str | None = None
    html: str | None = None
    error: str | None = None


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self.parts: list[str] = []
        self.links: list[str] = []
        self.published_at: datetime | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        attributes = {name.casefold(): value for name, value in attrs if value is not None}
        if normalized == "a" and (href := attributes.get("href")):
            self.links.append(href)
        if normalized == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").casefold()
            if key in {
                "article:published_time",
                "datepublished",
                "publishdate",
                "pubdate",
            }:
                self.published_at = _parse_datetime(attributes.get("content"))
        if normalized in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self.parts.append(data.strip())


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_html(html: str) -> _VisibleTextParser:
    parser = _VisibleTextParser()
    parser.feed(html)
    if parser.published_at is None:
        parser.published_at = _json_ld_published_at(html)
    return parser


def _json_ld_published_at(html: str) -> datetime | None:
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if isinstance(candidate, dict):
                parsed = _parse_datetime(candidate.get("datePublished"))
                if parsed is not None:
                    return parsed
    return None


class DrissionPageFetcher:
    def __init__(
        self,
        *,
        policy: UrlPolicy,
        timeout_seconds: float,
        base_timeout_seconds: float,
        script_timeout_seconds: float,
        quit_timeout_seconds: float,
        max_concurrency: int,
        worker_module: str,
        max_response_bytes: int,
        min_content_characters: int,
    ) -> None:
        self._policy = policy
        self._timeout_seconds = timeout_seconds
        self._base_timeout_seconds = base_timeout_seconds
        self._script_timeout_seconds = script_timeout_seconds
        self._quit_timeout_seconds = quit_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._worker_module = worker_module
        self._max_response_bytes = max_response_bytes
        self._min_content_characters = min_content_characters

    @property
    def name(self) -> str:
        return "drission-page"

    async def fetch(self, source: SourceRequest) -> FetchedDocument:
        if not isinstance(source, UrlSource):
            raise FetchError("DrissionPage only accepts URL sources.", retryable=False)
        source_url = await self._policy.validate(str(source.url))
        request = DrissionWorkerRequest(
            url=source_url,
            timeout_seconds=self._timeout_seconds,
            base_timeout_seconds=self._base_timeout_seconds,
            script_timeout_seconds=self._script_timeout_seconds,
            quit_timeout_seconds=self._quit_timeout_seconds,
            max_response_bytes=self._max_response_bytes,
            allow_private=self._policy.allow_private,
            allowed_ports=self._policy.allowed_ports,
        )
        try:
            async with self._semaphore:
                response = await run_json_worker(
                    command=(sys.executable, "-m", self._worker_module),
                    request=request,
                    response_type=DrissionWorkerResponse,
                    timeout_seconds=self._timeout_seconds + 10,
                )
        except TimeoutError as exc:
            raise FetchError("DrissionPage exceeded its configured deadline.") from exc
        except Exception as exc:
            raise FetchError("DrissionPage worker failed to execute.") from exc
        if not response.ok or not response.final_url or response.html is None:
            raise FetchError(response.error or "DrissionPage failed to render the source.")
        final_url = await self._policy.validate(response.final_url)
        parsed_html = _parse_html(response.html)
        visible = extract_html_content(response.html, final_url)
        article_text: str | None = None
        try:
            from trafilatura import bare_extraction

            article = bare_extraction(
                response.html,
                url=final_url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
                deduplicate=True,
            )
            extracted = (
                article.get("text") if isinstance(article, dict) else getattr(article, "text", None)
            )
            article_text = extracted if isinstance(extracted, str) else None
        except (ImportError, ValueError):
            pass
        content = choose_article_text(article_text, visible.text, self._min_content_characters)
        outbound_links = _absolute_http_links(parsed_html.links, final_url)
        if not content and visible.video_links:
            title = (response.title or "").strip()
            if title and title != "Untitled source":
                content = title
        if not content:
            raise FetchError("DrissionPage returned no readable content.")
        if (
            len(content) < self._min_content_characters
            and not outbound_links
            and not visible.video_links
        ):
            raise FetchError("DrissionPage returned insufficient visible content.")
        return FetchedDocument(
            source=SourceSnapshot(
                kind=SourceKind.URL,
                source_uri=source_url,
                final_uri=final_url,
                title=(response.title or "").strip() or "Untitled source",
                published_at=parsed_html.published_at,
                fetcher=self.name,
                content_sha256=sha256(content.encode("utf-8")).hexdigest(),
            ),
            content=content,
            outbound_links=outbound_links,
            video_links=visible.video_links,
        )

    async def aclose(self) -> None:
        return None


def _absolute_http_links(values: list[str], base_url: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for value in values:
        absolute = urljoin(base_url, value)
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
        if len(links) >= 500:
            break
    return links
