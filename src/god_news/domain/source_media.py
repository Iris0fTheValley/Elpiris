from __future__ import annotations

from collections.abc import AsyncIterable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Protocol
from uuid import UUID, uuid4

from pydantic import AnyHttpUrl, Field, StringConstraints, model_validator

from god_news.domain.models import DomainModel, NonBlankStr, utc_now
from god_news.sources.models import Attribution, RightsMetadata, SourceName

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class SourceVideoProbe(DomainModel):
    duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    video_codec: NonBlankStr
    audio_codec: NonBlankStr | None = None
    fps: float = Field(gt=0, le=240)


class SourceMediaArtifact(DomainModel):
    """Immutable evidence for one locally acquired source video.

    Unknown or permission-required rights never become publishable merely
    because bytes were downloaded for editorial review.
    """

    artifact_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    source: SourceName
    media_index: int = Field(ge=0)
    acquired_by: NonBlankStr
    source_url: AnyHttpUrl
    canonical_story_url: AnyHttpUrl
    attribution: Attribution
    rights: RightsMetadata
    publish_eligible: bool
    content_type: NonBlankStr
    filename: NonBlankStr
    sha256: Sha256Hex
    size_bytes: int = Field(gt=0)
    probe: SourceVideoProbe
    retrieved_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_publication_eligibility(self) -> SourceMediaArtifact:
        rights_allow_publication = (
            not self.rights.requires_human_review
            and self.rights.allows_republication is True
            and self.rights.allows_derivatives is not False
        )
        if self.publish_eligible != rights_allow_publication:
            raise ValueError("publish_eligible must be derived from the rights snapshot")
        return self


class StoredSourceMediaArtifact(SourceMediaArtifact):
    storage_key: NonBlankStr


class AcquireSourceMediaRequest(DomainModel):
    expected_story_version: int = Field(ge=1)
    media_index: int = Field(ge=0)
    requested_by: NonBlankStr


class SourceMediaRepository(Protocol):
    async def list_for_story(self, story_id: UUID) -> Sequence[StoredSourceMediaArtifact]: ...

    async def get_by_index(
        self,
        story_id: UUID,
        media_index: int,
    ) -> StoredSourceMediaArtifact | None: ...

    async def create(self, artifact: StoredSourceMediaArtifact) -> StoredSourceMediaArtifact: ...


class SourceMediaStore(Protocol):
    async def write_staged(
        self,
        *,
        story_id: UUID,
        artifact_id: UUID,
        content_type: str,
        body: AsyncIterable[bytes],
    ) -> tuple[str, str, int, Path]: ...

    async def promote(self, storage_key: str, *, story_id: UUID) -> Path: ...

    async def remove_staged(self, storage_key: str) -> None: ...

    async def write(
        self,
        *,
        story_id: UUID,
        artifact_id: UUID,
        content_type: str,
        body: AsyncIterable[bytes],
    ) -> tuple[str, str, int, Path]: ...

    async def remove(self, storage_key: str) -> None: ...

    async def resolve_verified(
        self,
        storage_key: str,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> Path: ...


class SourceMediaDownloader(Protocol):
    def stream(
        self,
        story_id: UUID,
        url: str,
    ) -> AbstractAsyncContextManager[tuple[str, AsyncIterable[bytes]]]: ...


class SourceVideoInspector(Protocol):
    async def inspect(self, path: Path) -> SourceVideoProbe: ...
