from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime
from html import unescape
from time import perf_counter
from typing import Generic, Literal, Protocol, TypeVar
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import ValidationError

from god_news.domain.models import FetchedDocument, UrlSource
from god_news.errors import FetchError, FetchPolicyError
from god_news.infrastructure.fetchers.chain import TracedFetchError
from god_news.infrastructure.fetchers.telemetry import (
    FetchLayerAttempt,
    TracedFetchResult,
)
from god_news.sources.collectors.models import (
    CollectionOutcome,
    CollectorReadiness,
    SourceCollectionRun,
)
from god_news.sources.collectors.support import (
    CollectorFailure,
    RunRecorder,
    readiness_failure,
    readiness_for,
)
from god_news.sources.models import (
    PikabuTextBlock,
    PikabuVideoBlock,
    RawDazhongItem,
    RawDazhongVideo,
    RawPikabuItem,
    RawRightsDeclaration,
    SourceName,
)

RawPublicItemT = TypeVar("RawPublicItemT", RawDazhongItem, RawPikabuItem)

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)", re.IGNORECASE)
_BARE_LINK = re.compile(r"https?://[^\s<>\])}\"']+", re.IGNORECASE)
_CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "verify you are human",
    "checking your browser",
    "проверка, что вы не робот",
    "проверка что вы не робот",
    "капча",
    "验证码",
    "人机验证",
    "安全验证",
    "ddos-guard",
    "access denied",
)


class TracedURLFetcher(Protocol):
    async def fetch_with_trace(self, source: UrlSource) -> TracedFetchResult: ...


def _contains_captcha(content: str) -> bool:
    normalized = " ".join(content.casefold().split())
    return any(marker in normalized for marker in _CAPTCHA_MARKERS)


def _canonical_candidate(value: str, base_url: str) -> str | None:
    decoded = unescape(value).strip().rstrip(".,;:!?")
    absolute = urljoin(base_url, decoded)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _links_from_document(document: FetchedDocument) -> list[str]:
    candidates = [str(link) for link in document.outbound_links]
    candidates.extend(match.group(1) for match in _MARKDOWN_LINK.finditer(document.content))
    candidates.extend(match.group(0) for match in _BARE_LINK.finditer(document.content))
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        canonical = _canonical_candidate(candidate, document.source.final_uri)
        if canonical and canonical not in seen:
            seen.add(canonical)
            unique.append(canonical)
    return unique


class AuthorizedPublicPageCollector(ABC, Generic[RawPublicItemT]):
    """Public-page adapter that never calls an undocumented site API."""

    source: SourceName

    def __init__(
        self,
        *,
        fetcher: TracedURLFetcher,
        endpoint: str,
        enabled: bool,
        public_page_use_authorized: bool,
        default_limit: int,
        allowed_host_suffixes: Sequence[str],
    ) -> None:
        self._fetcher = fetcher
        self._endpoint = endpoint
        self._enabled = enabled
        self._public_page_use_authorized = public_page_use_authorized
        self._default_limit = default_limit
        self._allowed_host_suffixes = tuple(
            suffix.casefold().lstrip(".") for suffix in allowed_host_suffixes
        )

    def readiness(self) -> CollectorReadiness:
        configured = bool(
            self._endpoint and self._allowed_host_suffixes and self._host_allowed(self._endpoint)
        )
        return readiness_for(
            source=self.source,
            enabled=self._enabled,
            configured=configured,
            authorized=configured and self._public_page_use_authorized,
            notes=[
                "authorized_public_pages_only",
                "no_undocumented_private_api",
                "stop_on_captcha",
                "per_item_rights_review_required",
            ],
        )

    async def collect(self, *, limit: int | None = None) -> SourceCollectionRun:
        recorder = RunRecorder(self.source)
        unavailable = readiness_failure(self.readiness())
        if unavailable is not None:
            return recorder.fail_readiness(unavailable)

        requested_limit = self._default_limit if limit is None else limit
        if not 1 <= requested_limit <= 50:
            recorder.errors.append(
                CollectorFailure(
                    "invalid_collection_limit",
                    "Public-page collection limit must be between 1 and 50.",
                ).evidence()
            )
            return recorder.finish(items=[])

        try:
            candidates = await self._discover(requested_limit, recorder)
        except CollectorFailure as exc:
            recorder.errors.append(exc.evidence())
            outcome: CollectionOutcome | None = None
            if exc.code == "captcha_detected":
                outcome = "stopped_captcha"
            elif exc.code == "access_challenge_detected":
                outcome = "stopped_access_challenge"
            return recorder.finish(items=[], outcome=outcome)

        items: list[RawPublicItemT] = []
        for candidate in candidates:
            try:
                document = await self._fetch(candidate, "item", recorder)
                self._raise_if_captcha(document, recorder, operation="item")
                started = perf_counter()
                item = self._map_document(document)
            except CollectorFailure as exc:
                recorder.errors.append(exc.evidence())
                if exc.code == "captcha_detected":
                    return recorder.finish(
                        items=list(items),
                        outcome="stopped_captcha",
                    )
                if exc.code == "access_challenge_detected":
                    return recorder.finish(
                        items=list(items),
                        outcome="stopped_access_challenge",
                    )
                continue
            except (ValidationError, ValueError):
                failure = CollectorFailure(
                    f"{self.source}_item_contract_invalid",
                    f"A {self.source} page did not satisfy the typed source contract.",
                )
                recorder.errors.append(failure.evidence())
                recorder.attempt(
                    layer=f"{self.source}-contract-mapper",
                    operation="item",
                    outcome="failed",
                    duration_ms=(perf_counter() - started) * 1_000,
                    error_code=failure.code,
                )
            else:
                items.append(item)
                recorder.attempt(
                    layer=f"{self.source}-contract-mapper",
                    operation="item",
                    outcome="succeeded",
                    duration_ms=(perf_counter() - started) * 1_000,
                    item_count=1,
                )
        if not items and not recorder.errors:
            recorder.errors.append(
                CollectorFailure(
                    "source_returned_no_items",
                    f"The configured {self.source} page returned no story items.",
                ).evidence()
            )
        return recorder.finish(items=list(items))

    async def _discover(self, limit: int, recorder: RunRecorder) -> list[str]:
        if self._is_story_url(self._endpoint):
            return [self._endpoint]
        document = await self._fetch(self._endpoint, "discover", recorder)
        self._raise_if_captcha(document, recorder, operation="discover")
        candidates = [
            url
            for url in _links_from_document(document)
            if self._host_allowed(url) and self._is_story_url(url)
        ]
        if not candidates:
            raise CollectorFailure(
                "story_links_not_found",
                f"The configured {self.source} page exposed no recognized story links.",
            )
        return candidates[:limit]

    async def _fetch(
        self,
        url: str,
        operation: Literal["discover", "item"],
        recorder: RunRecorder,
    ) -> FetchedDocument:
        try:
            result = await self._fetcher.fetch_with_trace(UrlSource(url=url))
        except TracedFetchError as exc:
            self._record_fetch_attempts(exc.attempts, operation, recorder)
            if any(attempt.error_code == "access_challenge_detected" for attempt in exc.attempts):
                raise CollectorFailure(
                    "access_challenge_detected",
                    (
                        f"The {self.source} provider refused automated access; "
                        "collection stopped without bypassing its access controls."
                    ),
                    retryable=False,
                ) from exc
            raise CollectorFailure(
                "public_page_fetch_failed",
                f"Every configured fetch layer failed for a {self.source} page.",
                retryable=exc.retryable,
            ) from exc
        except FetchPolicyError as exc:
            raise CollectorFailure(
                "public_page_url_rejected",
                f"A {self.source} page URL was rejected by the outbound policy.",
            ) from exc
        except FetchError as exc:
            raise CollectorFailure(
                "public_page_fetch_failed",
                f"The {self.source} page could not be fetched.",
                retryable=exc.retryable,
            ) from exc
        self._record_fetch_attempts(result.attempts, operation, recorder)
        if not self._host_allowed(result.document.source.final_uri):
            recorder.attempt(
                layer=f"{self.source}-host-guard",
                operation=operation,
                outcome="stopped",
                duration_ms=0,
                error_code="public_page_host_rejected",
                retryable=False,
            )
            raise CollectorFailure(
                "public_page_host_rejected",
                f"A redirected {self.source} page left its configured host allowlist.",
            )
        return result.document

    @staticmethod
    def _record_fetch_attempts(
        attempts: Sequence[FetchLayerAttempt],
        operation: Literal["discover", "item"],
        recorder: RunRecorder,
    ) -> None:
        for raw_attempt in attempts:
            recorder.attempt(
                layer=raw_attempt.layer,
                operation=operation,
                outcome=raw_attempt.outcome,
                duration_ms=raw_attempt.duration_ms,
                error_code=raw_attempt.error_code,
                retryable=raw_attempt.retryable,
            )

    def _raise_if_captcha(
        self,
        document: FetchedDocument,
        recorder: RunRecorder,
        *,
        operation: Literal["discover", "item"],
    ) -> None:
        if not _contains_captcha(document.content):
            return
        recorder.attempt(
            layer=f"{self.source}-captcha-guard",
            operation=operation,
            outcome="stopped",
            duration_ms=0,
            error_code="captcha_detected",
            retryable=False,
        )
        raise CollectorFailure(
            "captcha_detected",
            f"CAPTCHA or bot verification was detected; the {self.source} run stopped.",
        )

    def _host_allowed(self, url: str) -> bool:
        hostname = (urlsplit(url).hostname or "").casefold()
        return any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in self._allowed_host_suffixes
        )

    @abstractmethod
    def _is_story_url(self, url: str) -> bool: ...

    @abstractmethod
    def _map_document(self, document: FetchedDocument) -> RawPublicItemT: ...


class DazhongPublicPageCollector(AuthorizedPublicPageCollector[RawDazhongItem]):
    source: Literal["dazhong"] = "dazhong"

    def _is_story_url(self, url: str) -> bool:
        if not self._host_allowed(url):
            return False
        path = urlsplit(url).path.casefold()
        return "/share/general/" in path or "/general/" in path

    def _map_document(self, document: FetchedDocument) -> RawDazhongItem:
        published_at = self._require_published_at(document.source.published_at)
        path_parts = [part for part in urlsplit(document.source.final_uri).path.split("/") if part]
        if not path_parts:
            raise ValueError("missing Dazhong article identifier")
        return RawDazhongItem(
            article_id=path_parts[-1],
            url=document.source.final_uri,
            canonical_url=document.source.final_uri,
            title=document.source.title,
            body=document.content,
            author=document.source.author,
            publisher="大众新闻",
            published_at=published_at,
            language="zh-CN",
            channel=(path_parts[-3] if len(path_parts) >= 3 else None),
            media=[RawDazhongVideo(url=link) for link in document.video_links],
            rights=RawRightsDeclaration(
                status="permission_required",
                copyright_holder="大众新闻",
                allows_republication=None,
                allows_derivatives=None,
                requires_attribution=True,
            ),
        )

    @staticmethod
    def _require_published_at(value: datetime | None) -> datetime:
        if value is None:
            raise ValueError("Dazhong page did not expose a publication timestamp")
        return value


class PikabuPublicPageCollector(AuthorizedPublicPageCollector[RawPikabuItem]):
    source: Literal["pikabu"] = "pikabu"

    def _is_story_url(self, url: str) -> bool:
        if not self._host_allowed(url):
            return False
        return re.search(r"/story/[^/?#]+_\d+/?$", urlsplit(url).path, re.IGNORECASE) is not None

    def _map_document(self, document: FetchedDocument) -> RawPikabuItem:
        published_at = self._require_published_at(document.source.published_at)
        path = urlsplit(document.source.final_uri).path
        match = re.search(r"_(\d+)/?$", path)
        if match is None:
            raise ValueError("missing Pikabu story identifier")
        return RawPikabuItem(
            story_id=match.group(1),
            url=document.source.final_uri,
            title=document.source.title,
            author_username=document.source.author,
            published_at=published_at,
            blocks=[
                PikabuTextBlock(text=document.content),
                *(PikabuVideoBlock(url=link) for link in document.video_links),
            ],
            language="ru",
            publisher="Pikabu",
            rights=RawRightsDeclaration(
                status="permission_required",
                copyright_holder=document.source.author,
                terms_url="https://pikabu.ru/information",
                allows_republication=None,
                allows_derivatives=None,
                requires_attribution=True,
            ),
        )

    @staticmethod
    def _require_published_at(value: datetime | None) -> datetime:
        if value is None:
            raise ValueError("Pikabu page did not expose a publication timestamp")
        return value
