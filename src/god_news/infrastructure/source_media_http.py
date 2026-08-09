from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

import httpx

from god_news.domain.source_media import SourceMediaDownloader
from god_news.errors import FetchPolicyError, SourceMediaAcquisitionError
from god_news.infrastructure.fetchers.url_policy import UrlPolicy


class HttpSourceMediaDownloader(SourceMediaDownloader):
    """Stream stored source URLs without following unvalidated redirects."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        policy: UrlPolicy,
        *,
        max_attempts: int = 4,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._client = client
        self._policy = policy
        self._max_attempts = max_attempts

    @asynccontextmanager
    async def stream(
        self,
        story_id: UUID,
        url: str,
    ) -> AsyncIterator[tuple[str, AsyncIterable[bytes]]]:
        try:
            await self._policy.validate(url)
        except FetchPolicyError as exc:
            raise SourceMediaAcquisitionError(
                story_id,
                "Source media URL was rejected by the outbound request policy.",
            ) from exc
        try:
            async with AsyncExitStack() as stack:
                response: httpx.Response | None = None
                last_error: httpx.HTTPError | None = None
                for attempt in range(1, self._max_attempts + 1):
                    try:
                        response = await stack.enter_async_context(
                            self._client.stream(
                                "GET",
                                url,
                                headers={
                                    "Accept": "video/mp4,application/octet-stream;q=0.8"
                                },
                            )
                        )
                        break
                    except httpx.HTTPError as exc:
                        last_error = exc
                        if attempt < self._max_attempts:
                            await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
                if response is None:
                    raise SourceMediaAcquisitionError(
                        story_id,
                        "Source media server could not be reached after retrying.",
                        status_code=502,
                        retryable=True,
                    ) from last_error
                if response.is_redirect:
                    raise SourceMediaAcquisitionError(
                        story_id,
                        "Source media redirected; the redirected URL must be captured and "
                        "reviewed first.",
                    )
                if response.status_code != 200:
                    raise SourceMediaAcquisitionError(
                        story_id,
                        "Source media server returned an unsuccessful status.",
                        status_code=502,
                        retryable=response.status_code >= 500 or response.status_code == 429,
                    )
                content_type = response.headers.get("content-type", "application/octet-stream")
                yield content_type, self._resumable_body(story_id, url, response)
        except SourceMediaAcquisitionError:
            raise
        except httpx.HTTPError as exc:
            raise SourceMediaAcquisitionError(
                story_id,
                "Source media server could not be reached.",
                status_code=502,
                retryable=True,
            ) from exc

    async def _resumable_body(
        self,
        story_id: UUID,
        url: str,
        initial_response: httpx.Response,
    ) -> AsyncIterator[bytes]:
        expected_size = _content_length(initial_response)
        offset = 0
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, self._max_attempts + 1):
            if attempt == 1:
                response = initial_response
                try:
                    async for chunk in response.aiter_bytes():
                        offset += len(chunk)
                        yield chunk
                except httpx.HTTPError as exc:
                    last_error = exc
            else:
                await asyncio.sleep(min(0.25 * (2 ** (attempt - 2)), 2.0))
                headers = {
                    "Accept": "video/mp4,application/octet-stream;q=0.8",
                    "Range": f"bytes={offset}-",
                }
                try:
                    async with self._client.stream("GET", url, headers=headers) as response:
                        _validate_resume_response(
                            story_id,
                            response,
                            expected_start=offset,
                            expected_size=expected_size,
                        )
                        if expected_size is None:
                            expected_size = _content_range_total(response)
                        async for chunk in response.aiter_bytes():
                            offset += len(chunk)
                            yield chunk
                    last_error = None
                except SourceMediaAcquisitionError:
                    raise
                except httpx.HTTPError as exc:
                    last_error = exc

            if expected_size is None:
                if last_error is None:
                    return
            elif offset == expected_size:
                return
            elif offset > expected_size:
                raise SourceMediaAcquisitionError(
                    story_id,
                    "Source media server returned more bytes than declared.",
                    status_code=502,
                    retryable=False,
                )

        raise SourceMediaAcquisitionError(
            story_id,
            "Source media transfer ended before the declared byte length.",
            status_code=502,
            retryable=True,
        ) from last_error


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _content_range_total(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-range", "")
    match = re.fullmatch(r"bytes \d+-\d+/(\d+|\*)", raw)
    if match is None or match.group(1) == "*":
        return None
    return int(match.group(1))


def _validate_resume_response(
    story_id: UUID,
    response: httpx.Response,
    *,
    expected_start: int,
    expected_size: int | None,
) -> None:
    if response.is_redirect:
        raise SourceMediaAcquisitionError(
            story_id,
            "Source media resume redirected; the redirected URL must be reviewed first.",
        )
    if response.status_code != 206:
        raise SourceMediaAcquisitionError(
            story_id,
            "Source media server did not honor a safe byte-range resume.",
            status_code=502,
            retryable=response.status_code >= 500 or response.status_code == 429,
        )
    raw = response.headers.get("content-range", "")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", raw)
    if match is None or int(match.group(1)) != expected_start:
        raise SourceMediaAcquisitionError(
            story_id,
            "Source media server returned an invalid byte range.",
            status_code=502,
        )
    total = None if match.group(3) == "*" else int(match.group(3))
    if expected_size is not None and total != expected_size:
        raise SourceMediaAcquisitionError(
            story_id,
            "Source media byte length changed during resume.",
            status_code=502,
        )
