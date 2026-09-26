from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from god_news.api.app import create_app
from god_news.application.source_media import SourceMediaService
from god_news.demo.adapters import InMemoryStoryRepository
from god_news.domain.enums import StoryStatus
from god_news.domain.models import (
    FetchedDocument,
    ScriptPreferences,
    SourceItemIngestRequest,
    Story,
)
from god_news.domain.source_media import (
    AcquireSourceMediaRequest,
    SourceMediaRepository,
    SourceVideoProbe,
    StoredSourceMediaArtifact,
)
from god_news.errors import ConcurrentWriteError, FetchPolicyError, SourceMediaAcquisitionError
from god_news.infrastructure.database import Database
from god_news.infrastructure.source_media_http import HttpSourceMediaDownloader
from god_news.infrastructure.source_media_repository import SqlAlchemySourceMediaRepository
from god_news.infrastructure.source_media_store import LocalSourceMediaStore
from god_news.sources.models import parse_raw_source_json
from god_news.sources.registry import create_default_source_registry

from .conftest import Stack

FIXTURE = Path(__file__).parent / "fixtures" / "sources" / "reddit.json"
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def _story() -> Story:
    raw = parse_raw_source_json(FIXTURE.read_bytes())
    normalized = create_default_source_registry().normalize(raw)
    fetched = FetchedDocument.from_normalized_source(normalized)
    return Story(
        status=StoryStatus.FETCHED,
        source=fetched.source,
        provenance=normalized,
        original_text=fetched.content,
        target_language="zh-CN",
        preferences=ScriptPreferences(
            style="concise",
            target_duration_seconds=20,
            speaker_id="narrator",
        ),
    )


class _InMemorySourceMediaRepository(SourceMediaRepository):
    def __init__(self) -> None:
        self.items: dict[tuple[UUID, int], StoredSourceMediaArtifact] = {}

    async def list_for_story(self, story_id: UUID) -> Sequence[StoredSourceMediaArtifact]:
        return sorted(
            (item for (owner, _), item in self.items.items() if owner == story_id),
            key=lambda item: item.media_index,
        )

    async def get_by_index(
        self,
        story_id: UUID,
        media_index: int,
    ) -> StoredSourceMediaArtifact | None:
        return self.items.get((story_id, media_index))

    async def create(self, artifact: StoredSourceMediaArtifact) -> StoredSourceMediaArtifact:
        return self.items.setdefault((artifact.story_id, artifact.media_index), artifact)


class _Downloader:
    def __init__(self, payload: bytes = MP4_BYTES) -> None:
        self.payload = payload
        self.calls = 0

    @asynccontextmanager
    async def stream(
        self,
        story_id: UUID,
        url: str,
    ) -> AsyncIterator[tuple[str, AsyncIterable[bytes]]]:
        del story_id, url
        self.calls += 1

        async def chunks() -> AsyncIterator[bytes]:
            yield self.payload

        yield "video/mp4", chunks()


class _Inspector:
    async def inspect(self, path: Path) -> SourceVideoProbe:
        assert await asyncio.to_thread(path.read_bytes) == MP4_BYTES
        return SourceVideoProbe(
            duration_ms=42_000,
            width=720,
            height=1_280,
            video_codec="h264",
            audio_codec=None,
            fps=30,
        )


class _CancelledInspector:
    async def inspect(self, path: Path) -> SourceVideoProbe:
        assert await asyncio.to_thread(path.is_file)
        raise asyncio.CancelledError


class _PausedDownloader(_Downloader):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    @asynccontextmanager
    async def stream(
        self,
        story_id: UUID,
        url: str,
    ) -> AsyncIterator[tuple[str, AsyncIterable[bytes]]]:
        del story_id, url
        self.calls += 1

        async def chunks() -> AsyncIterator[bytes]:
            self.entered.set()
            await self.resume.wait()
            yield self.payload

        yield "video/mp4", chunks()


class _PausedInspector(_Inspector):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def inspect(self, path: Path) -> SourceVideoProbe:
        assert path.suffix == ".partial"
        self.entered.set()
        await self.resume.wait()
        return await super().inspect(path)


async def _one_chunk(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


class _UrlPolicy:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject

    async def validate(self, url: str) -> None:
        del url
        if self.reject:
            raise FetchPolicyError("blocked")


class _InterruptedStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._payload
        raise httpx.ReadError("connection interrupted")


@pytest.mark.asyncio
async def test_source_video_acquisition_is_idempotent_and_keeps_reddit_review_only(
    tmp_path: Path,
) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    repository = _InMemorySourceMediaRepository()
    downloader = _Downloader()
    service = SourceMediaService(
        stories=stories,
        repository=repository,
        store=LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024),
        downloader=downloader,
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )
    request = AcquireSourceMediaRequest(
        expected_story_version=story.version,
        media_index=1,
        requested_by="editor",
    )

    first = await service.acquire(story.story_id, request)
    second = await service.acquire(story.story_id, request)

    assert first == second
    assert downloader.calls == 1
    assert first.sha256 != "0" * 64
    assert first.probe.duration_ms == 42_000
    assert first.probe.audio_codec is None
    assert first.rights.status == "unknown"
    assert first.publish_eligible is False
    assert not hasattr(first, "storage_key")
    stored = await repository.get_by_index(story.story_id, 1)
    assert stored is not None
    assert (tmp_path / "media" / stored.storage_key).read_bytes() == MP4_BYTES


@pytest.mark.asyncio
async def test_slow_source_acquisition_releases_shared_asset_lock_until_publish(
    tmp_path: Path,
) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    repository = _InMemorySourceMediaRepository()
    downloader = _PausedDownloader()
    inspector = _PausedInspector()
    shared_lock = asyncio.Lock()
    root = tmp_path / "media"
    service = SourceMediaService(
        stories=stories,
        repository=repository,
        store=LocalSourceMediaStore(root, max_download_bytes=1024),
        downloader=downloader,
        inspector=inspector,
        asset_lifecycle_lock=shared_lock,
    )
    request = AcquireSourceMediaRequest(
        expected_story_version=story.version,
        media_index=1,
        requested_by="editor",
    )
    first = asyncio.create_task(service.acquire(story.story_id, request))
    second = asyncio.create_task(service.acquire(story.story_id, request))
    try:
        await asyncio.wait_for(downloader.entered.wait(), timeout=2)
        await asyncio.wait_for(shared_lock.acquire(), timeout=2)
        shared_lock.release()
        assert downloader.calls == 1

        downloader.resume.set()
        await asyncio.wait_for(inspector.entered.wait(), timeout=2)
        await asyncio.wait_for(shared_lock.acquire(), timeout=2)
        assert list(root.rglob("*.partial"))
        assert list(root.rglob("*.mp4")) == []
        inspector.resume.set()
        await asyncio.sleep(0)
        assert not first.done()
        shared_lock.release()

        assert await first == await second
        assert downloader.calls == 1
        assert list(root.rglob("*.partial")) == []
        assert len(list(root.rglob("*.mp4"))) == 1
        assert len(await repository.list_for_story(story.story_id)) == 1
    finally:
        downloader.resume.set()
        inspector.resume.set()
        if shared_lock.locked():
            shared_lock.release()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_source_media_repository_failure_cleans_staged_and_promoted_bytes(
    tmp_path: Path,
) -> None:
    class _FailOnceRepository(_InMemorySourceMediaRepository):
        failed = False

        async def create(self, artifact: StoredSourceMediaArtifact) -> StoredSourceMediaArtifact:
            if not self.failed:
                self.failed = True
                raise RuntimeError("database unavailable")
            return await super().create(artifact)

    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    repository = _FailOnceRepository()
    root = tmp_path / "media"
    service = SourceMediaService(
        stories=stories,
        repository=repository,
        store=LocalSourceMediaStore(root, max_download_bytes=1024),
        downloader=_Downloader(),
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )
    request = AcquireSourceMediaRequest(
        expected_story_version=story.version,
        media_index=1,
        requested_by="editor",
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.acquire(story.story_id, request)
    assert list(root.rglob("*.partial")) == []
    assert list(root.rglob("*.mp4")) == []

    await service.acquire(story.story_id, request)
    assert len(list(root.rglob("*.mp4"))) == 1


@pytest.mark.asyncio
async def test_cancellation_during_claim_keeps_published_file_with_its_row(
    tmp_path: Path,
) -> None:
    class _PausedRepository(_InMemorySourceMediaRepository):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.resume = asyncio.Event()

        async def create(self, artifact: StoredSourceMediaArtifact) -> StoredSourceMediaArtifact:
            self.entered.set()
            await self.resume.wait()
            return await super().create(artifact)

    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    repository = _PausedRepository()
    root = tmp_path / "media"
    service = SourceMediaService(
        stories=stories,
        repository=repository,
        store=LocalSourceMediaStore(root, max_download_bytes=1024),
        downloader=_Downloader(),
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )
    task = asyncio.create_task(
        service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=1,
                requested_by="editor",
            ),
        )
    )
    try:
        await asyncio.wait_for(repository.entered.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        repository.resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(await repository.list_for_story(story.story_id)) == 1
        assert len(list(root.rglob("*.mp4"))) == 1
        assert list(root.rglob("*.partial")) == []
    finally:
        repository.resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_story_version_change_during_probe_rejects_staged_download(
    tmp_path: Path,
) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    repository = _InMemorySourceMediaRepository()
    inspector = _PausedInspector()
    root = tmp_path / "media"
    service = SourceMediaService(
        stories=stories,
        repository=repository,
        store=LocalSourceMediaStore(root, max_download_bytes=1024),
        downloader=_Downloader(),
        inspector=inspector,
        asset_lifecycle_lock=asyncio.Lock(),
    )
    task = asyncio.create_task(
        service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=1,
                requested_by="editor",
            ),
        )
    )
    try:
        await asyncio.wait_for(inspector.entered.wait(), timeout=2)
        await stories.save(story, expected_version=story.version)
        inspector.resume.set()
        with pytest.raises(ConcurrentWriteError):
            await task
        assert await repository.list_for_story(story.story_id) == []
        assert list(root.rglob("*.partial")) == []
        assert list(root.rglob("*.mp4")) == []
    finally:
        inspector.resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_source_media_rejects_stale_versions_and_non_video_assets(tmp_path: Path) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    service = SourceMediaService(
        stories=stories,
        repository=_InMemorySourceMediaRepository(),
        store=LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024),
        downloader=_Downloader(),
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )

    with pytest.raises(ConcurrentWriteError):
        await service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version + 1,
                media_index=1,
                requested_by="editor",
            ),
        )
    with pytest.raises(SourceMediaAcquisitionError, match="not a video"):
        await service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=0,
                requested_by="editor",
            ),
        )


@pytest.mark.asyncio
async def test_missing_ffprobe_blocks_only_new_acquisition(tmp_path: Path) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    downloader = _Downloader()
    service = SourceMediaService(
        stories=stories,
        repository=_InMemorySourceMediaRepository(),
        store=LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024),
        downloader=downloader,
        inspector=None,
        asset_lifecycle_lock=asyncio.Lock(),
    )

    assert await service.list(story.story_id) == []
    with pytest.raises(SourceMediaAcquisitionError, match="ffprobe") as error:
        await service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=1,
                requested_by="editor",
            ),
        )
    assert error.value.status_code == 503
    assert downloader.calls == 0


@pytest.mark.asyncio
async def test_source_media_store_rejects_invalid_or_oversized_payloads_atomically(
    tmp_path: Path,
) -> None:
    story_id = _story().story_id
    store = LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024)

    with pytest.raises(SourceMediaAcquisitionError, match="MP4 file signature"):
        await store.write(
            story_id=story_id,
            artifact_id=UUID("840908d9-1970-4c8d-b625-f458df19aa76"),
            content_type="video/mp4",
            body=_one_chunk(b"not an mp4"),
        )
    small_store = LocalSourceMediaStore(tmp_path / "small", max_download_bytes=12)
    with pytest.raises(SourceMediaAcquisitionError, match="download limit"):
        await small_store.write(
            story_id=story_id,
            artifact_id=UUID("5bd2fe26-a023-4f8d-9fe4-6cecb88e6c52"),
            content_type="video/mp4",
            body=_one_chunk(MP4_BYTES),
        )

    assert await asyncio.to_thread(lambda: list(tmp_path.rglob("*.partial"))) == []
    assert await asyncio.to_thread(lambda: list(tmp_path.rglob("*.mp4"))) == []


@pytest.mark.asyncio
async def test_source_media_http_adapter_streams_and_maps_boundary_failures() -> None:
    story_id = _story().story_id

    async def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=MP4_BYTES,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as client:
        downloader = HttpSourceMediaDownloader(client, _UrlPolicy())  # type: ignore[arg-type]
        async with downloader.stream(story_id, "https://media.example/video.mp4") as (
            content_type,
            body,
        ):
            received = b"".join([chunk async for chunk in body])
        assert content_type == "application/octet-stream"
        assert received == MP4_BYTES

    async def unavailable_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable_handler)) as client:
        downloader = HttpSourceMediaDownloader(client, _UrlPolicy())  # type: ignore[arg-type]
        with pytest.raises(SourceMediaAcquisitionError) as unavailable:
            async with downloader.stream(story_id, "https://media.example/video.mp4"):
                pass
        assert unavailable.value.status_code == 502
        assert unavailable.value.retryable is True

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as client:
        downloader = HttpSourceMediaDownloader(  # type: ignore[arg-type]
            client,
            _UrlPolicy(reject=True),
        )
        with pytest.raises(SourceMediaAcquisitionError, match="outbound request policy"):
            async with downloader.stream(story_id, "https://media.example/video.mp4"):
                pass


@pytest.mark.asyncio
async def test_source_media_http_adapter_resumes_an_interrupted_declared_body() -> None:
    story_id = _story().story_id
    split = 12
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4", "content-length": str(len(MP4_BYTES))},
                stream=_InterruptedStream(MP4_BYTES[:split]),
                request=request,
            )
        assert request.headers["range"] == f"bytes={split}-"
        return httpx.Response(
            206,
            headers={
                "content-type": "video/mp4",
                "content-range": f"bytes {split}-{len(MP4_BYTES) - 1}/{len(MP4_BYTES)}",
                "content-length": str(len(MP4_BYTES) - split),
            },
            content=MP4_BYTES[split:],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = HttpSourceMediaDownloader(  # type: ignore[arg-type]
            client,
            _UrlPolicy(),
            max_attempts=2,
        )
        async with downloader.stream(story_id, "https://media.example/video.mp4") as (
            content_type,
            body,
        ):
            received = b"".join([chunk async for chunk in body])

    assert content_type == "video/mp4"
    assert received == MP4_BYTES
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_source_media_http_adapter_retries_initial_connection_failure() -> None:
    story_id = _story().story_id
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("temporary connection failure", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=MP4_BYTES,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = HttpSourceMediaDownloader(  # type: ignore[arg-type]
            client,
            _UrlPolicy(),
            max_attempts=2,
        )
        async with downloader.stream(story_id, "https://media.example/video.mp4") as (
            _,
            body,
        ):
            received = b"".join([chunk async for chunk in body])

    assert received == MP4_BYTES
    assert calls == 2


@pytest.mark.asyncio
async def test_source_media_http_adapter_retries_transient_http_status() -> None:
    story_id = _story().story_id
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=MP4_BYTES, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = HttpSourceMediaDownloader(  # type: ignore[arg-type]
            client,
            _UrlPolicy(),
            max_attempts=2,
        )
        async with downloader.stream(story_id, "https://media.example/video.mp4") as (
            _,
            body,
        ):
            received = b"".join([chunk async for chunk in body])

    assert received == MP4_BYTES
    assert calls == 2


@pytest.mark.asyncio
async def test_source_media_http_adapter_refuses_unsafe_full_body_resume() -> None:
    story_id = _story().story_id
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4", "content-length": str(len(MP4_BYTES))},
                stream=_InterruptedStream(MP4_BYTES[:8]),
                request=request,
            )
        return httpx.Response(200, content=MP4_BYTES, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = HttpSourceMediaDownloader(  # type: ignore[arg-type]
            client,
            _UrlPolicy(),
            max_attempts=2,
        )
        with pytest.raises(SourceMediaAcquisitionError, match="safe byte-range resume"):
            async with downloader.stream(story_id, "https://media.example/video.mp4") as (
                _,
                body,
            ):
                _ = b"".join([chunk async for chunk in body])


@pytest.mark.asyncio
async def test_source_media_cancellation_removes_downloaded_bytes(tmp_path: Path) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    repository = _InMemorySourceMediaRepository()
    service = SourceMediaService(
        stories=stories,
        repository=repository,
        store=LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024),
        downloader=_Downloader(),
        inspector=_CancelledInspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=1,
                requested_by="editor",
            ),
        )

    assert await repository.list_for_story(story.story_id) == []
    assert await asyncio.to_thread(lambda: list(tmp_path.rglob("*.mp4"))) == []


@pytest.mark.asyncio
async def test_source_media_cancellation_during_download_removes_partial_bytes(
    tmp_path: Path,
) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    downloader = _PausedDownloader()
    service = SourceMediaService(
        stories=stories,
        repository=_InMemorySourceMediaRepository(),
        store=LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024),
        downloader=downloader,
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )
    task = asyncio.create_task(
        service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=1,
                requested_by="editor",
            ),
        )
    )
    await asyncio.wait_for(downloader.entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(lambda: list(tmp_path.rglob("*.partial"))) == []
    assert await asyncio.to_thread(lambda: list(tmp_path.rglob("*.mp4"))) == []


@pytest.mark.asyncio
async def test_source_media_refuses_tampered_persisted_evidence(tmp_path: Path) -> None:
    story = _story()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    repository = _InMemorySourceMediaRepository()
    store = LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024)
    service = SourceMediaService(
        stories=stories,
        repository=repository,
        store=store,
        downloader=_Downloader(),
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )
    artifact = await service.acquire(
        story.story_id,
        AcquireSourceMediaRequest(
            expected_story_version=story.version,
            media_index=1,
            requested_by="editor",
        ),
    )
    stored = await repository.get_by_index(story.story_id, 1)
    assert stored is not None
    path = tmp_path / "media" / stored.storage_key
    await asyncio.to_thread(path.write_bytes, MP4_BYTES + b"tampered")

    with pytest.raises(SourceMediaAcquisitionError, match="missing or invalid"):
        await service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=1,
                requested_by="editor",
            ),
        )
    with pytest.raises(SourceMediaAcquisitionError, match="missing or invalid"):
        await service.media_path(story.story_id, artifact.artifact_id)


@pytest.mark.asyncio
async def test_sql_source_media_repository_persists_and_protects_storage_path(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'media.db').as_posix()}")
    await database.create_schema()
    repository = SqlAlchemySourceMediaRepository(
        database.sessions,
        storage_root=tmp_path / "assets",
    )
    story = _story()
    in_memory = _InMemorySourceMediaRepository()
    stories = InMemoryStoryRepository()
    await stories.create(story)
    service = SourceMediaService(
        stories=stories,
        repository=in_memory,
        store=LocalSourceMediaStore(tmp_path / "assets", max_download_bytes=1024),
        downloader=_Downloader(),
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )
    try:
        await service.acquire(
            story.story_id,
            AcquireSourceMediaRequest(
                expected_story_version=story.version,
                media_index=1,
                requested_by="editor",
            ),
        )
        stored = await in_memory.get_by_index(story.story_id, 1)
        assert stored is not None
        await repository.create(stored)

        reloaded = await repository.get_by_index(story.story_id, 1)
        assert reloaded == stored
        assert await repository.protected_asset_paths() == [
            (tmp_path / "assets" / stored.storage_key).resolve()
        ]
    finally:
        await database.aclose()


@pytest.mark.asyncio
async def test_source_media_api_acquires_lists_and_streams_without_local_paths(
    stack: Stack,
    tmp_path: Path,
) -> None:
    raw = parse_raw_source_json(FIXTURE.read_bytes())
    story = await stack.workflow.ingest_source_item(SourceItemIngestRequest(item=raw))
    stack.container.source_media = SourceMediaService(
        stories=stack.repository,
        repository=_InMemorySourceMediaRepository(),
        store=LocalSourceMediaStore(tmp_path / "media", max_download_bytes=1024),
        downloader=_Downloader(),
        inspector=_Inspector(),
        asset_lifecycle_lock=asyncio.Lock(),
    )

    async def factory(settings):  # type: ignore[no-untyped-def]
        del settings
        return stack.container

    app = create_app(stack.settings, container_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            acquired = await client.post(
                f"/api/v1/stories/{story.story_id}/source-media/acquire",
                json={
                    "expected_story_version": story.version,
                    "media_index": 1,
                    "requested_by": "editor",
                },
            )
            assert acquired.status_code == 201
            payload = acquired.json()
            assert payload["publish_eligible"] is False
            assert "storage_key" not in payload

            listing = await client.get(f"/api/v1/stories/{story.story_id}/source-media")
            assert listing.status_code == 200
            assert listing.json() == [payload]

            content = await client.get(
                f"/api/v1/stories/{story.story_id}/source-media/{payload['artifact_id']}/content"
            )
            assert content.status_code == 200
            assert content.content == MP4_BYTES
