from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from god_news.domain.enums import StoryStatus
from god_news.domain.models import (
    AudioBundle,
    ClassificationMetrics,
    FetchedDocument,
    MemoryItem,
    MemoryQuery,
    MemoryWrite,
    ProductionManifest,
    RegeneratedSpokenSegment,
    ReviewRecord,
    ScriptDocument,
    ScriptDraft,
    ScriptPreferences,
    SourceRequest,
    StateTransition,
    Story,
    TranslationResult,
)


class Fetcher(Protocol):
    @property
    def name(self) -> str: ...

    async def fetch(self, source: SourceRequest) -> FetchedDocument: ...

    async def aclose(self) -> None: ...


class TextGenerator(Protocol):
    async def healthcheck(self) -> None: ...

    async def translate_and_summarize(
        self,
        *,
        story_id: UUID,
        source_title: str,
        content: str,
        source_language: str | None,
        target_language: str,
        memories: Sequence[MemoryItem],
        response_cache: bool = True,
    ) -> TranslationResult: ...

    async def create_script(
        self,
        *,
        story_id: UUID,
        translation: TranslationResult,
        preferences: ScriptPreferences,
        memories: Sequence[MemoryItem],
    ) -> ScriptDraft: ...

    async def regenerate_spoken_segment(
        self,
        *,
        story_id: UUID,
        segment_id: UUID,
        caption_text: str,
        caption_language: str,
        spoken_language: str,
        memories: Sequence[MemoryItem],
    ) -> RegeneratedSpokenSegment: ...

    async def aclose(self) -> None: ...


class MemoryProvider(Protocol):
    async def healthcheck(self) -> None: ...

    async def recall(self, query: MemoryQuery) -> Sequence[MemoryItem]: ...

    async def remember(self, memory: MemoryWrite) -> None: ...

    async def aclose(self) -> None: ...


class SpeechSynthesizer(Protocol):
    async def healthcheck(self) -> None: ...

    async def synthesize(self, story_id: UUID, script: ScriptDocument) -> AudioBundle: ...

    async def aclose(self) -> None: ...


class VideoRenderer(Protocol):
    """Reserved extension point. No implementation is wired in the current phase."""

    async def render(self, manifest: ProductionManifest) -> str: ...


class StoryRepository(Protocol):
    async def create(self, story: Story) -> Story: ...

    async def get(self, story_id: UUID) -> Story: ...

    async def list(
        self,
        *,
        status: StoryStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> Sequence[Story]: ...

    async def save(
        self,
        story: Story,
        *,
        expected_version: int,
        transition_reason: str | None = None,
        review: ReviewRecord | None = None,
    ) -> Story: ...

    async def add_review(self, review: ReviewRecord) -> None: ...

    async def get_review(self, review_id: UUID) -> ReviewRecord | None: ...

    async def list_reviews(self, story_id: UUID) -> Sequence[ReviewRecord]: ...

    async def list_transitions(self, story_id: UUID) -> Sequence[StateTransition]: ...

    async def classification_metrics(self) -> ClassificationMetrics: ...

    async def healthcheck(self) -> None: ...
