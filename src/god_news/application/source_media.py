from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from god_news.domain.ports import StoryRepository
from god_news.domain.source_media import (
    AcquireSourceMediaRequest,
    SourceMediaArtifact,
    SourceMediaDownloader,
    SourceMediaRepository,
    SourceMediaStore,
    SourceVideoInspector,
    StoredSourceMediaArtifact,
)
from god_news.errors import ConcurrentWriteError, SourceMediaAcquisitionError
from god_news.sources.models import RightsMetadata


class SourceMediaService:
    """Manual, idempotent acquisition of immutable source-video evidence."""

    def __init__(
        self,
        *,
        stories: StoryRepository,
        repository: SourceMediaRepository,
        store: SourceMediaStore,
        downloader: SourceMediaDownloader,
        inspector: SourceVideoInspector | None,
        asset_lifecycle_lock: asyncio.Lock,
    ) -> None:
        self._stories = stories
        self._repository = repository
        self._store = store
        self._downloader = downloader
        self._inspector = inspector
        self._asset_lifecycle_lock = asset_lifecycle_lock
        # Serialize acquisitions for this service without holding the shared
        # asset lock across network transfer and ffprobe.
        self._acquisition_lock = asyncio.Lock()

    async def list(self, story_id: UUID) -> list[SourceMediaArtifact]:
        await self._stories.get(story_id)
        return [
            SourceMediaArtifact.model_validate(item.model_dump(exclude={"storage_key"}))
            for item in await self._repository.list_for_story(story_id)
        ]

    async def acquire(
        self,
        story_id: UUID,
        request: AcquireSourceMediaRequest,
    ) -> SourceMediaArtifact:
        async with self._acquisition_lock:
            return await self._acquire_serialized(story_id, request)

    async def _acquire_serialized(
        self,
        story_id: UUID,
        request: AcquireSourceMediaRequest,
    ) -> SourceMediaArtifact:
        story = await self._stories.get(story_id)
        if story.version != request.expected_story_version:
            raise ConcurrentWriteError(story_id)
        provenance = story.provenance
        if provenance is None:
            raise SourceMediaAcquisitionError(
                story_id,
                "Story has no normalized source-media provenance.",
                status_code=409,
            )
        if request.media_index >= len(provenance.media):
            raise SourceMediaAcquisitionError(story_id, "Source media index does not exist.")
        media = provenance.media[request.media_index]
        if media.kind != "video":
            raise SourceMediaAcquisitionError(
                story_id,
                "Selected source media is not a video.",
            )

        existing = await self._repository.get_by_index(story_id, request.media_index)
        if existing is not None:
            await self._resolve_verified_file(story_id, existing)
            return SourceMediaArtifact.model_validate(
                existing.model_dump(exclude={"storage_key"})
            )
        inspector = self._inspector
        if inspector is None:
            raise SourceMediaAcquisitionError(
                story_id,
                "Source video acquisition is unavailable because ffprobe is missing.",
                status_code=503,
                retryable=True,
            )

        artifact_id = uuid4()
        storage_key: str | None = None
        commit_started = False
        try:
            async with self._downloader.stream(story_id, str(media.url)) as (
                content_type,
                body,
            ):
                storage_key, digest, size_bytes, path = await self._store.write_staged(
                    story_id=story_id,
                    artifact_id=artifact_id,
                    content_type=content_type,
                    body=body,
                )
            try:
                probe = await inspector.inspect(path)
            except (OSError, ValueError, TimeoutError) as exc:
                raise SourceMediaAcquisitionError(
                    story_id,
                    "Downloaded source media is not a decodable video.",
                ) from exc
            stored = StoredSourceMediaArtifact(
                artifact_id=artifact_id,
                story_id=story_id,
                source=provenance.source,
                media_index=request.media_index,
                acquired_by=request.requested_by,
                source_url=media.url,
                canonical_story_url=provenance.canonical_url,
                attribution=provenance.attribution,
                rights=provenance.rights,
                publish_eligible=_publish_eligible(provenance.rights),
                # The store and ffprobe both proved the bytes are an MP4. Normalize
                # generic origin responses so browser review remains playable.
                content_type="video/mp4",
                filename=f"source-{request.media_index}.mp4",
                sha256=digest,
                size_bytes=size_bytes,
                probe=probe,
                storage_key=storage_key,
            )
            commit = asyncio.create_task(self._publish_staged(story_id, request, stored))
            commit_started = True
            try:
                saved = await asyncio.shield(commit)
            except asyncio.CancelledError:
                # Once promotion starts, let the claim or its cleanup finish.
                # A cancelled HTTP request may still leave a valid claimed asset.
                try:
                    await commit
                except Exception:
                    pass
                raise
            return SourceMediaArtifact.model_validate(
                saved.model_dump(exclude={"storage_key"})
            )
        except (Exception, asyncio.CancelledError):
            if storage_key is not None and not commit_started:
                await self._store.remove_staged(storage_key)
            raise

    async def _publish_staged(
        self,
        story_id: UUID,
        request: AcquireSourceMediaRequest,
        stored: StoredSourceMediaArtifact,
    ) -> StoredSourceMediaArtifact:
        promoted = False
        async with self._asset_lifecycle_lock:
            try:
                current = await self._stories.get(story_id)
                if current.version != request.expected_story_version:
                    raise ConcurrentWriteError(story_id)
                existing = await self._repository.get_by_index(story_id, request.media_index)
                if existing is not None:
                    await self._resolve_verified_file(story_id, existing)
                    return existing
                promoted = True
                await self._store.promote(stored.storage_key, story_id=story_id)
                saved = await self._repository.create(stored)
                if saved.artifact_id != stored.artifact_id:
                    await self._store.remove(stored.storage_key)
                return saved
            except (Exception, asyncio.CancelledError):
                if promoted:
                    await self._store.remove(stored.storage_key)
                raise
            finally:
                await self._store.remove_staged(stored.storage_key)

    async def media_path(
        self,
        story_id: UUID,
        artifact_id: UUID,
    ) -> tuple[SourceMediaArtifact, Path]:
        for item in await self._repository.list_for_story(story_id):
            if item.artifact_id == artifact_id:
                path = await self._resolve_verified_file(story_id, item)
                public = SourceMediaArtifact.model_validate(
                    item.model_dump(exclude={"storage_key"})
                )
                return public, path
        raise SourceMediaAcquisitionError(
            story_id,
            "Source media artifact was not found.",
            status_code=404,
        )

    async def _resolve_verified_file(
        self,
        story_id: UUID,
        artifact: StoredSourceMediaArtifact,
    ) -> Path:
        try:
            return await self._store.resolve_verified(
                artifact.storage_key,
                expected_sha256=artifact.sha256,
                expected_size_bytes=artifact.size_bytes,
            )
        except (OSError, ValueError) as exc:
            raise SourceMediaAcquisitionError(
                story_id,
                "Stored source media evidence is missing or invalid.",
                status_code=409,
            ) from exc


def _publish_eligible(rights: RightsMetadata) -> bool:
    return (
        not rights.requires_human_review
        and rights.allows_republication is True
        and rights.allows_derivatives is not False
    )
