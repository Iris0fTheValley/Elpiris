from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape
from time import monotonic, perf_counter
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from god_news.infrastructure.fetchers.html_content import direct_video_links
from god_news.sources.collectors.models import (
    CollectorDiagnostic,
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
    RawRedditItem,
    RawRedditPreviewImage,
    RawRedditVideo,
    RawRightsDeclaration,
)


class _UpstreamModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _OAuthToken(_UpstreamModel):
    access_token: str = Field(min_length=1)
    token_type: str = "bearer"
    expires_in: int = Field(default=3_600, gt=0)


class _PreviewSource(_UpstreamModel):
    url: str
    width: int | None = None
    height: int | None = None


class _PreviewImage(_UpstreamModel):
    source: _PreviewSource


class _RedditVideoPayload(_UpstreamModel):
    fallback_url: str
    duration: int | None = None


class _Preview(_UpstreamModel):
    images: list[_PreviewImage] = Field(default_factory=list)
    reddit_video_preview: _RedditVideoPayload | None = None


class _SecureMedia(_UpstreamModel):
    reddit_video: _RedditVideoPayload | None = None


class _PostData(_UpstreamModel):
    id: str = Field(min_length=1)
    permalink: str = Field(min_length=1)
    title: str = Field(min_length=1)
    selftext: str = ""
    author: str | None = None
    created_utc: float
    subreddit: str = Field(min_length=1)
    score: int = 0
    num_comments: int = Field(default=0, ge=0)
    link_flair_text: str | None = None
    over_18: bool = False
    spoiler: bool = False
    locked: bool = False
    is_self: bool = True
    url_overridden_by_dest: str | None = None
    url: str | None = None
    secure_media: _SecureMedia | None = None
    media: _SecureMedia | None = None
    preview: _Preview | None = None


class _Child(_UpstreamModel):
    kind: Literal["t3"] = "t3"
    data: _PostData


class _ListingData(_UpstreamModel):
    children: list[_Child] = Field(default_factory=list)
    after: str | None = None


class _Listing(_UpstreamModel):
    data: _ListingData


class RedditOAuthCollector:
    source: Literal["reddit"] = "reddit"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        token_endpoint: str,
        client_id: SecretStr | None,
        client_secret: SecretStr | None,
        user_agent: str | None,
        enabled: bool,
        api_use_authorized: bool,
        subreddit: str,
        default_limit: int,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._client = client
        self._endpoint = endpoint.rstrip("/")
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_agent = user_agent.strip() if user_agent else None
        self._enabled = enabled
        self._api_use_authorized = api_use_authorized
        self._subreddit = subreddit
        self._default_limit = default_limit
        self._clock = clock
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    def readiness(self) -> CollectorReadiness:
        configured = bool(
            self._client_id and self._client_secret and self._user_agent and self._subreddit
        )
        return readiness_for(
            source=self.source,
            enabled=self._enabled,
            configured=configured,
            authorized=configured and self._api_use_authorized,
            notes=[
                "official_oauth_data_api_only",
                "operator_authorization_includes_downstream_ai_and_video_use",
                "per_item_rights_review_required",
            ],
        )

    async def diagnose(self) -> CollectorDiagnostic:
        """Validate OAuth credentials without listing or downloading user content."""

        readiness = self.readiness()
        if readiness.state in {"disabled", "unconfigured"}:
            failure = readiness_failure(readiness)
            assert failure is not None
            return CollectorDiagnostic(
                source=self.source,
                outcome=readiness.state,
                credentials_verified=False if readiness.configured else None,
                endpoint_reachable=None,
                errors=[failure.evidence()],
            )
        recorder = RunRecorder(self.source)
        try:
            await self._token(recorder)
        except CollectorFailure as exc:
            return CollectorDiagnostic(
                source=self.source,
                outcome="failed",
                credentials_verified=False,
                endpoint_reachable=(exc.code != "reddit_oauth_unreachable"),
                attempts=recorder.attempts,
                errors=[exc.evidence()],
            )
        return CollectorDiagnostic(
            source=self.source,
            outcome="verified",
            credentials_verified=True,
            endpoint_reachable=True,
            attempts=recorder.attempts,
        )

    async def collect(self, *, limit: int | None = None) -> SourceCollectionRun:
        recorder = RunRecorder(self.source)
        unavailable = readiness_failure(self.readiness())
        if unavailable is not None:
            return recorder.fail_readiness(unavailable)

        requested_limit = self._default_limit if limit is None else limit
        if not 1 <= requested_limit <= 100:
            recorder.errors.append(
                CollectorFailure(
                    "invalid_collection_limit",
                    "Reddit collection limit must be between 1 and 100.",
                ).evidence()
            )
            return recorder.finish(items=[])

        items: list[RawRedditItem] = []
        seen_posts: set[str] = set()
        seen_cursors: set[str] = set()
        after: str | None = None
        try:
            token = await self._token(recorder)
            # The item budget also bounds pagination; ten pages cap API calls when
            # listings repeatedly return fewer posts than requested.
            for _ in range(min(requested_limit, 10)):
                listing, rate_exhausted = await self._listing(
                    token, requested_limit - len(items), recorder, after=after
                )
                for child in listing.data.children:
                    post = child.data
                    if post.id in seen_posts:
                        continue
                    seen_posts.add(post.id)
                    started = perf_counter()
                    try:
                        item = self._map_post(post)
                    except (ValidationError, ValueError):
                        failure = CollectorFailure(
                            "reddit_item_contract_invalid",
                            "A Reddit result did not satisfy the typed source contract.",
                        )
                        recorder.errors.append(failure.evidence())
                        recorder.attempt(
                            layer="reddit-contract-mapper",
                            operation="item",
                            outcome="failed",
                            duration_ms=(perf_counter() - started) * 1_000,
                            error_code=failure.code,
                        )
                    else:
                        items.append(item)
                        recorder.attempt(
                            layer="reddit-contract-mapper",
                            operation="item",
                            outcome="succeeded",
                            duration_ms=(perf_counter() - started) * 1_000,
                            item_count=1,
                        )
                    if len(items) >= requested_limit:
                        break
                cursor = listing.data.after
                if (
                    len(items) >= requested_limit
                    or not listing.data.children
                    or not cursor
                    or cursor in seen_cursors
                    or rate_exhausted
                ):
                    break
                seen_cursors.add(cursor)
                after = cursor
        except CollectorFailure as exc:
            recorder.errors.append(exc.evidence())
        if not items and not recorder.errors:
            recorder.errors.append(
                CollectorFailure(
                    "source_returned_no_items",
                    "Reddit returned no posts for the configured subreddit.",
                ).evidence()
            )
        return recorder.finish(items=list(items))

    async def _token(self, recorder: RunRecorder) -> str:
        async with self._token_lock:
            if self._access_token and self._clock() < self._access_token_expires_at:
                recorder.attempt(
                    layer="reddit-oauth-cache",
                    operation="authenticate",
                    outcome="succeeded",
                    duration_ms=0,
                )
                return self._access_token

            assert self._client_id is not None
            assert self._client_secret is not None
            assert self._user_agent is not None
            started = perf_counter()
            try:
                response = await self._client.post(
                    self._token_endpoint,
                    data={"grant_type": "client_credentials"},
                    auth=httpx.BasicAuth(
                        self._client_id.get_secret_value(),
                        self._client_secret.get_secret_value(),
                    ),
                    headers={
                        "Accept": "application/json",
                        "User-Agent": self._user_agent,
                    },
                )
            except httpx.HTTPError as exc:
                failure = CollectorFailure(
                    "reddit_oauth_unreachable",
                    "Reddit OAuth could not be reached.",
                    retryable=True,
                )
                recorder.attempt(
                    layer="reddit-oauth",
                    operation="authenticate",
                    outcome="failed",
                    duration_ms=(perf_counter() - started) * 1_000,
                    error_code=failure.code,
                    retryable=True,
                )
                raise failure from exc
            if response.status_code != 200:
                failure = CollectorFailure(
                    "reddit_oauth_rejected",
                    "Reddit OAuth rejected the configured client credentials.",
                    retryable=response.status_code >= 500 or response.status_code == 429,
                    http_status=response.status_code,
                )
                recorder.attempt(
                    layer="reddit-oauth",
                    operation="authenticate",
                    outcome="failed",
                    duration_ms=(perf_counter() - started) * 1_000,
                    http_status=response.status_code,
                    error_code=failure.code,
                    retryable=failure.retryable,
                )
                raise failure
            try:
                payload = _OAuthToken.model_validate_json(response.content)
            except ValidationError as exc:
                failure = CollectorFailure(
                    "reddit_oauth_contract_invalid",
                    "Reddit OAuth returned an unexpected response shape.",
                )
                recorder.attempt(
                    layer="reddit-oauth",
                    operation="authenticate",
                    outcome="failed",
                    duration_ms=(perf_counter() - started) * 1_000,
                    http_status=response.status_code,
                    error_code=failure.code,
                )
                raise failure from exc
            if payload.token_type.casefold() != "bearer":
                failure = CollectorFailure(
                    "reddit_oauth_contract_invalid",
                    "Reddit OAuth returned an unsupported token type.",
                )
                recorder.attempt(
                    layer="reddit-oauth",
                    operation="authenticate",
                    outcome="failed",
                    duration_ms=(perf_counter() - started) * 1_000,
                    http_status=response.status_code,
                    error_code=failure.code,
                )
                raise failure
            self._access_token = payload.access_token
            self._access_token_expires_at = self._clock() + max(payload.expires_in - 30, 1)
            recorder.attempt(
                layer="reddit-oauth",
                operation="authenticate",
                outcome="succeeded",
                duration_ms=(perf_counter() - started) * 1_000,
                http_status=response.status_code,
            )
            return payload.access_token

    async def _listing(
        self,
        token: str,
        limit: int,
        recorder: RunRecorder,
        *,
        after: str | None,
    ) -> tuple[_Listing, bool]:
        assert self._user_agent is not None
        started = perf_counter()
        try:
            response = await self._client.get(
                f"{self._endpoint}/r/{self._subreddit}/new",
                params={
                    "limit": limit,
                    "raw_json": 1,
                    **({"after": after} if after is not None else {}),
                },
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": self._user_agent,
                },
            )
        except httpx.HTTPError as exc:
            failure = CollectorFailure(
                "reddit_api_unreachable",
                "Reddit Data API could not be reached.",
                retryable=True,
            )
            recorder.attempt(
                layer="reddit-data-api",
                operation="listing",
                outcome="failed",
                duration_ms=(perf_counter() - started) * 1_000,
                error_code=failure.code,
                retryable=True,
            )
            raise failure from exc
        if response.status_code != 200:
            failure = CollectorFailure(
                "reddit_api_http_error",
                "Reddit Data API returned an unsuccessful status.",
                retryable=response.status_code >= 500 or response.status_code == 429,
                http_status=response.status_code,
            )
            recorder.attempt(
                layer="reddit-data-api",
                operation="listing",
                outcome="failed",
                duration_ms=(perf_counter() - started) * 1_000,
                http_status=response.status_code,
                error_code=failure.code,
                retryable=failure.retryable,
            )
            raise failure
        try:
            listing = _Listing.model_validate_json(response.content)
        except ValidationError as exc:
            failure = CollectorFailure(
                "reddit_api_contract_invalid",
                "Reddit Data API returned an unexpected response shape.",
            )
            recorder.attempt(
                layer="reddit-data-api",
                operation="listing",
                outcome="failed",
                duration_ms=(perf_counter() - started) * 1_000,
                http_status=response.status_code,
                error_code=failure.code,
            )
            raise failure from exc
        recorder.attempt(
            layer="reddit-data-api",
            operation="listing",
            outcome="succeeded",
            duration_ms=(perf_counter() - started) * 1_000,
            http_status=response.status_code,
            item_count=len(listing.data.children),
        )
        remaining = response.headers.get("X-Ratelimit-Remaining")
        try:
            rate_exhausted = remaining is not None and float(remaining) < 1
        except ValueError:
            rate_exhausted = False
        return listing, rate_exhausted

    @staticmethod
    def _map_post(post: _PostData) -> RawRedditItem:
        preview_images: list[RawRedditPreviewImage] = []
        if post.preview is not None:
            for image in post.preview.images[:20]:
                preview_images.append(
                    RawRedditPreviewImage(
                        url=unescape(image.source.url),
                        width=image.source.width,
                        height=image.source.height,
                    )
                )

        video = None
        for candidate in (
            post.secure_media.reddit_video if post.secure_media else None,
            post.media.reddit_video if post.media else None,
            post.preview.reddit_video_preview if post.preview else None,
        ):
            if candidate is None:
                continue
            direct_links = direct_video_links([candidate.fallback_url], post.permalink)
            if direct_links:
                thumbnail = str(preview_images[0].url) if preview_images else None
                video = RawRedditVideo(
                    fallback_url=direct_links[0],
                    thumbnail_url=thumbnail,
                    duration_ms=(candidate.duration * 1_000 if candidate.duration else None),
                )
                break

        outbound_candidate = post.url_overridden_by_dest or post.url
        outbound_url: str | None = None
        if not post.is_self and outbound_candidate:
            parts = urlsplit(unescape(outbound_candidate))
            if parts.scheme in {"http", "https"} and parts.hostname:
                outbound_url = unescape(outbound_candidate)

        if video is None and outbound_url is not None:
            direct_links = direct_video_links([outbound_url], outbound_url)
            if direct_links:
                video = RawRedditVideo(fallback_url=direct_links[0])

        return RawRedditItem(
            post_id=post.id,
            permalink=post.permalink,
            title=post.title,
            selftext=post.selftext,
            author=post.author,
            created_utc=datetime.fromtimestamp(post.created_utc, tz=UTC),
            subreddit=post.subreddit,
            score=post.score,
            num_comments=post.num_comments,
            link_flair_text=post.link_flair_text,
            over_18=post.over_18,
            spoiler=post.spoiler,
            locked=post.locked,
            is_self=post.is_self,
            outbound_url=outbound_url,
            video=video,
            preview_images=preview_images,
            rights=RawRightsDeclaration(
                status="permission_required",
                copyright_holder=post.author,
                terms_url="https://redditinc.com/policies/data-api-terms",
                allows_republication=None,
                allows_derivatives=None,
                requires_attribution=True,
            ),
        )
