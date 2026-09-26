from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from god_news.domain.enums import SourceKind
from god_news.domain.models import FetchedDocument, SourceRequest, SourceSnapshot, UrlSource
from god_news.errors import FetchError
from god_news.infrastructure.fetchers.html_content import video_links_from_text
from god_news.infrastructure.fetchers.url_policy import UrlPolicy
from god_news.sources.text import normalize_text


class _JinaData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str
    title: str | None = None
    url: str | None = None
    author: str | None = None
    published_time: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("publishedTime", "published_time"),
    )
    http_status: int | None = Field(
        default=None,
        validation_alias=AliasChoices("httpStatus", "http_status"),
    )

    @field_validator("published_time", mode="before")
    @classmethod
    def parse_published_time(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None


class _JinaPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: int
    data: _JinaData


class JinaReaderFetcher:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        policy: UrlPolicy,
        base_url: str,
        api_key: str | None,
        page_timeout_seconds: int,
        max_response_bytes: int,
        min_content_characters: int,
    ) -> None:
        self._client = client
        self._policy = policy
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._page_timeout_seconds = page_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._min_content_characters = min_content_characters

    @property
    def name(self) -> str:
        return "jina-reader"

    async def fetch(self, source: SourceRequest) -> FetchedDocument:
        if not isinstance(source, UrlSource):
            raise FetchError("Jina Reader only accepts URL sources.", retryable=False)
        source_url = await self._policy.validate(str(source.url))
        headers = {
            "Accept": "application/json",
            "X-Timeout": str(self._page_timeout_seconds),
            "X-Retain-Images": "none",
            "X-Retain-Links": "text",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            if "#" in source_url:
                payload_bytes = await self._read_limited(
                    method="POST",
                    url=f"{self._base_url}/",
                    headers=headers,
                    form_url=source_url,
                )
            else:
                payload_bytes = await self._read_limited(
                    method="GET",
                    url=f"{self._base_url}/{source_url}",
                    headers=headers,
                )
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in {429, 502, 503, 504}
            raise FetchError(
                f"Jina Reader returned HTTP {exc.response.status_code}.",
                retryable=retryable,
            ) from exc
        except httpx.HTTPError as exc:
            raise FetchError("Jina Reader could not be reached.") from exc
        try:
            payload = _JinaPayload.model_validate_json(payload_bytes)
        except ValidationError as exc:
            raise FetchError("Jina Reader returned an unexpected response shape.") from exc
        if payload.code != 200 or payload.data.http_status not in {None, 200}:
            raise FetchError("Jina Reader did not fetch a successful source response.")
        content = normalize_text(payload.data.content)
        if len(content) < self._min_content_characters:
            raise FetchError("Jina Reader returned insufficient article content.")
        final_url = await self._policy.validate(payload.data.url or source_url)
        digest = sha256(content.encode("utf-8")).hexdigest()
        return FetchedDocument(
            source=SourceSnapshot(
                kind=SourceKind.URL,
                source_uri=source_url,
                final_uri=final_url,
                title=(payload.data.title or "").strip() or "Untitled source",
                author=payload.data.author,
                published_at=payload.data.published_time,
                fetcher=self.name,
                content_sha256=digest,
            ),
            content=content,
            video_links=video_links_from_text(content, final_url),
        )

    async def aclose(self) -> None:
        # The shared HTTP client is owned by the application container.
        return None

    async def _read_limited(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        form_url: str | None = None,
    ) -> bytes:
        if form_url is None:
            context = self._client.stream(method, url, headers=headers)
        else:
            context = self._client.stream(
                method,
                url,
                headers=headers,
                data={"url": form_url},
            )
        payload = bytearray()
        async with context as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > self._max_response_bytes:
                    raise FetchError("Jina Reader response exceeded the configured size limit.")
        return bytes(payload)
