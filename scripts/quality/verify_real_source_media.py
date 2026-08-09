from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx

from god_news.application.source_media import SourceMediaService
from god_news.demo.adapters import InMemoryStoryRepository
from god_news.domain.enums import StoryStatus
from god_news.domain.models import FetchedDocument, ScriptPreferences, Story
from god_news.domain.source_media import (
    AcquireSourceMediaRequest,
    SourceMediaRepository,
    StoredSourceMediaArtifact,
)
from god_news.infrastructure.fetchers.url_policy import UrlPolicy
from god_news.infrastructure.source_media_http import HttpSourceMediaDownloader
from god_news.infrastructure.source_media_probe import FFprobeSourceVideoInspector
from god_news.infrastructure.source_media_store import LocalSourceMediaStore
from god_news.sources.models import parse_raw_source_json
from god_news.sources.registry import create_default_source_registry

WORKSPACE = Path(__file__).resolve().parents[2]
REAL_FIXTURE = WORKSPACE / "tests" / "fixtures" / "sources" / "pikabu_video.json"


class InMemorySourceMediaRepository(SourceMediaRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, int], StoredSourceMediaArtifact] = {}

    async def list_for_story(self, story_id: UUID) -> Sequence[StoredSourceMediaArtifact]:
        return [
            item
            for (owner, _), item in sorted(self._items.items(), key=lambda pair: pair[0][1])
            if owner == story_id
        ]

    async def get_by_index(
        self,
        story_id: UUID,
        media_index: int,
    ) -> StoredSourceMediaArtifact | None:
        return self._items.get((story_id, media_index))

    async def create(self, artifact: StoredSourceMediaArtifact) -> StoredSourceMediaArtifact:
        return self._items.setdefault((artifact.story_id, artifact.media_index), artifact)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download one real allowed-source video through the production acquisition "
            "boundary, verify immutable bytes with ffprobe, and emit a rights-aware report."
        )
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=WORKSPACE / "output" / "source-media-smoke",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    raw = parse_raw_source_json(REAL_FIXTURE.read_bytes())
    normalized = create_default_source_registry().normalize(raw)
    document = FetchedDocument.from_normalized_source(normalized)
    story = Story(
        status=StoryStatus.FETCHED,
        title=normalized.title,
        source=document.source,
        provenance=normalized,
        original_text=normalized.content_text,
        target_language="zh-CN",
        preferences=ScriptPreferences(
            style="warm concise bulletin",
            target_duration_seconds=20,
            speaker_id="verification-narrator",
        ),
    )
    stories = InMemoryStoryRepository()
    await stories.create(story)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = (args.output_root / timestamp).resolve()
    run_root.mkdir(parents=True, exist_ok=False)
    ffprobe = FFprobeSourceVideoInspector.discover(WORKSPACE)
    if ffprobe is None:
        raise RuntimeError("ffprobe is required for real source-media verification.")

    timeout = httpx.Timeout(connect=15, read=120, write=15, pool=15)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": (
                "god-news/0.1 "
                "(https://github.com/Iris0fTheValley/god-news; contact: repository issues)"
            )
        },
    ) as client:
        repository = InMemorySourceMediaRepository()
        service = SourceMediaService(
            stories=stories,
            repository=repository,
            store=LocalSourceMediaStore(run_root / "media", max_download_bytes=128 * 1024 * 1024),
            downloader=HttpSourceMediaDownloader(client, UrlPolicy()),
            inspector=FFprobeSourceVideoInspector(ffprobe, timeout_seconds=60),
            asset_lifecycle_lock=asyncio.Lock(),
        )
        request = AcquireSourceMediaRequest(
            expected_story_version=story.version,
            media_index=0,
            requested_by="real-source-smoke",
        )
        first = await service.acquire(story.story_id, request)
        second = await service.acquire(story.story_id, request)
        if first != second:
            raise RuntimeError("Repeated source-media acquisition was not idempotent.")
        verified, media_path = await service.media_path(story.story_id, first.artifact_id)
        if verified != first:
            raise RuntimeError("Verified source-media evidence changed after persistence.")
        if first.publish_eligible:
            raise RuntimeError("Permission-required source media became publishable.")
        if first.probe.audio_codec is None:
            raise RuntimeError("The real source video does not contain an audio stream.")

    report = {
        "schema_version": 1,
        "verified_at": datetime.now(UTC).isoformat(),
        "fixture": str(REAL_FIXTURE),
        "canonical_story_url": str(first.canonical_story_url),
        "source_url": str(first.source_url),
        "local_path": str(media_path),
        "sha256": first.sha256,
        "size_bytes": first.size_bytes,
        "content_type": first.content_type,
        "probe": first.probe.model_dump(mode="json"),
        "rights": first.rights.model_dump(mode="json"),
        "publish_eligible": first.publish_eligible,
        "idempotent_reacquire": True,
    }
    report_path = run_root / "verification-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(report_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
