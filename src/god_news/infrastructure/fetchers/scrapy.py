from __future__ import annotations

import sys
from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from god_news.domain.enums import SourceKind
from god_news.domain.models import FetchedDocument, SourceRequest, SourceSnapshot, UrlSource
from god_news.errors import FetchError
from god_news.infrastructure.fetchers.url_policy import UrlPolicy
from god_news.infrastructure.processes import run_json_worker


class ScrapyWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    allow_private: bool
    allowed_ports: tuple[int, ...]
    max_response_bytes: int
    min_content_characters: int
    download_timeout_seconds: int
    redirect_max_times: int
    retry_times: int
    depth_limit: int
    close_page_count: int
    user_agent: str


class ScrapyWorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    final_url: str | None = None
    title: str | None = None
    content: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    outbound_links: list[str] = Field(default_factory=list, max_length=500)
    video_links: list[str] = Field(default_factory=list, max_length=50)
    http_status: int | None = None
    error_code: str | None = None
    error: str | None = None


class ScrapyTrafilaturaFetcher:
    def __init__(
        self,
        *,
        policy: UrlPolicy,
        timeout_seconds: float,
        worker_module: str,
        max_response_bytes: int,
        min_content_characters: int,
        download_timeout_seconds: int,
        redirect_max_times: int,
        retry_times: int,
        depth_limit: int,
        close_page_count: int,
        user_agent: str,
    ) -> None:
        self._policy = policy
        self._timeout_seconds = timeout_seconds
        self._worker_module = worker_module
        self._max_response_bytes = max_response_bytes
        self._min_content_characters = min_content_characters
        self._download_timeout_seconds = download_timeout_seconds
        self._redirect_max_times = redirect_max_times
        self._retry_times = retry_times
        self._depth_limit = depth_limit
        self._close_page_count = close_page_count
        self._user_agent = user_agent

    @property
    def name(self) -> str:
        return "scrapy-trafilatura"

    async def fetch(self, source: SourceRequest) -> FetchedDocument:
        if not isinstance(source, UrlSource):
            raise FetchError("Scrapy only accepts URL sources.", retryable=False)
        source_url = await self._policy.validate(str(source.url))
        request = ScrapyWorkerRequest(
            url=source_url,
            allow_private=self._policy.allow_private,
            allowed_ports=self._policy.allowed_ports,
            max_response_bytes=self._max_response_bytes,
            min_content_characters=self._min_content_characters,
            download_timeout_seconds=self._download_timeout_seconds,
            redirect_max_times=self._redirect_max_times,
            retry_times=self._retry_times,
            depth_limit=self._depth_limit,
            close_page_count=self._close_page_count,
            user_agent=self._user_agent,
        )
        try:
            response = await run_json_worker(
                command=(sys.executable, "-m", self._worker_module),
                request=request,
                response_type=ScrapyWorkerResponse,
                timeout_seconds=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise FetchError("Scrapy worker exceeded its configured deadline.") from exc
        except Exception as exc:
            raise FetchError("Scrapy worker failed to execute.") from exc
        if not response.ok or not response.content or not response.final_url:
            raise FetchError(
                response.error or "Scrapy did not return article content.",
                retryable=response.error_code != "access_challenge_detected",
                code=response.error_code or "fetch_failed",
            )
        final_url = await self._policy.validate(response.final_url)
        content = response.content.strip()
        if (
            len(content) < self._min_content_characters
            and not response.outbound_links
            and not response.video_links
        ):
            raise FetchError("Trafilatura returned insufficient article content.")
        return FetchedDocument(
            source=SourceSnapshot(
                kind=SourceKind.URL,
                source_uri=source_url,
                final_uri=final_url,
                title=(response.title or "").strip() or "Untitled source",
                author=response.author,
                published_at=response.published_at,
                fetcher=self.name,
                content_sha256=sha256(content.encode("utf-8")).hexdigest(),
            ),
            content=content,
            outbound_links=response.outbound_links,
            video_links=response.video_links,
        )

    async def aclose(self) -> None:
        return None
