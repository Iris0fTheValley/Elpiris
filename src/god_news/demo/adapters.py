"""Deterministic adapters for the offline demo and contract tests."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import struct
import wave
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from god_news.domain.deduplication import story_identity
from god_news.domain.enums import (
    AudioFormat,
    ContentCategory,
    SourceKind,
    SpeechEmotion,
    StoryStatus,
)
from god_news.domain.fsm import validate_story_invariants, validate_transition_evidence
from god_news.domain.language import same_language, should_preserve_chinese_source
from god_news.domain.models import (
    AudioBundle,
    AudioClip,
    ClassificationMetrics,
    EditorialScreening,
    FetchedDocument,
    MemoryItem,
    MemoryQuery,
    MemoryWrite,
    RegeneratedSpokenSegment,
    ReviewRecord,
    ScriptDocument,
    ScriptDraft,
    ScriptPreferences,
    ScriptSegmentDraft,
    SourceRequest,
    SourceSnapshot,
    StateTransition,
    Story,
    SynthesisMetadata,
    TextSource,
    TranslationResult,
)
from god_news.errors import (
    ConcurrentWriteError,
    DuplicateStoryError,
    StoryNotFoundError,
    TTSGenerationError,
)
from god_news.voice_profiles import ResolvedVoiceProfile, VoiceReference


class DeterministicFetcher:
    """Offline URL/text adapter used by the demo launcher and contract tests."""

    @property
    def name(self) -> str:
        return "deterministic-offline"

    async def fetch(self, source: SourceRequest) -> FetchedDocument:
        if isinstance(source, TextSource):
            return FetchedDocument.from_text(source)
        uri = str(source.url)
        content = (
            "Offline fixture: a neighbor found a lost dog during heavy rain, "
            "contacted the owner, and waited until the family arrived safely."
        )
        return FetchedDocument(
            source=SourceSnapshot(
                kind=SourceKind.URL,
                source_uri=uri,
                final_uri=uri,
                title="Offline URL fixture: a safe return home",
                detected_language="en",
                fetcher=self.name,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            ),
            content=content,
        )

    async def aclose(self) -> None:
        return None


class DeterministicVoiceProfileResolver:
    """Offline voice-catalogue adapter for the browser demo.

    It accepts one fully configured synthetic narrator. This keeps the
    workflow's first-review boundary active in deterministic environments
    without coupling those environments to the role repository.
    """

    def __init__(self, asset_root: Path, *, speaker_id: str = "narrator") -> None:
        reference = VoiceReference(
            audio_path=asset_root / "offline-reference.wav",
            text="Offline deterministic voice reference.",
        )
        self._profile = ResolvedVoiceProfile(
            profile_id=uuid4(),
            speaker_id=speaker_id,
            model_profile="deterministic-offline",
            gpt_weights_path=asset_root / "offline-gpt.ckpt",
            sovits_weights_path=asset_root / "offline-sovits.pth",
            emotion_refs={emotion: reference for emotion in SpeechEmotion},
            default_emotion=SpeechEmotion.HAPPINESS,
        )

    async def resolve_voice(self, speaker_id: str) -> ResolvedVoiceProfile | None:
        if speaker_id == self._profile.speaker_id:
            return self._profile
        return None


class InMemoryStoryRepository:
    def __init__(self) -> None:
        self._stories: dict[UUID, Story] = {}
        self._reviews: dict[UUID, list[ReviewRecord]] = {}
        self._transitions: dict[UUID, list[StateTransition]] = {}
        self._lock = asyncio.Lock()

    async def create(self, story: Story) -> Story:
        validate_story_invariants(story)
        async with self._lock:
            if story.story_id in self._stories:
                raise ConcurrentWriteError(story.story_id)
            candidate = story_identity(story)
            for existing in self._stories.values():
                identity = story_identity(existing)
                if (
                    candidate.canonical_url_sha256 is not None
                    and identity.canonical_url_sha256 == candidate.canonical_url_sha256
                ):
                    raise DuplicateStoryError(existing.story_id, "canonical URL")
                if identity.content_sha256 == candidate.content_sha256:
                    raise DuplicateStoryError(existing.story_id, "normalized content")
            self._stories[story.story_id] = copy.deepcopy(story)
        return copy.deepcopy(story)

    async def get(self, story_id: UUID) -> Story:
        async with self._lock:
            story = self._stories.get(story_id)
            if story is None:
                raise StoryNotFoundError(story_id)
            return copy.deepcopy(story)

    async def list(
        self,
        *,
        status: StoryStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> Sequence[Story]:
        async with self._lock:
            stories = sorted(self._stories.values(), key=lambda item: item.updated_at, reverse=True)
            if status is not None:
                stories = [story for story in stories if story.status is status]
            elif not include_archived:
                stories = [story for story in stories if story.status is not StoryStatus.ARCHIVED]
            return copy.deepcopy(stories[offset : offset + limit])

    async def save(
        self,
        story: Story,
        *,
        expected_version: int,
        transition_reason: str | None = None,
        review: ReviewRecord | None = None,
    ) -> Story:
        validate_story_invariants(story)
        async with self._lock:
            current = self._stories.get(story.story_id)
            if current is None:
                raise StoryNotFoundError(story.story_id)
            if current.version != expected_version:
                raise ConcurrentWriteError(story.story_id)
            if current.status is not story.status:
                validate_transition_evidence(current.status, story.status, story, review)
            saved = story.model_copy(update={"version": expected_version + 1})
            self._stories[story.story_id] = copy.deepcopy(saved)
            if current.status is not saved.status:
                self._transitions.setdefault(story.story_id, []).append(
                    StateTransition(
                        story_id=story.story_id,
                        from_status=current.status,
                        to_status=saved.status,
                        reason=transition_reason or "pipeline transition",
                    )
                )
            if review is not None:
                self._reviews.setdefault(story.story_id, []).append(copy.deepcopy(review))
            return copy.deepcopy(saved)

    async def add_review(self, review: ReviewRecord) -> None:
        async with self._lock:
            if review.story_id not in self._stories:
                raise StoryNotFoundError(review.story_id)
            self._reviews.setdefault(review.story_id, []).append(copy.deepcopy(review))

    async def list_reviews(self, story_id: UUID) -> Sequence[ReviewRecord]:
        async with self._lock:
            if story_id not in self._stories:
                raise StoryNotFoundError(story_id)
            return copy.deepcopy(self._reviews.get(story_id, []))

    async def get_review(self, review_id: UUID) -> ReviewRecord | None:
        async with self._lock:
            for reviews in self._reviews.values():
                for review in reviews:
                    if review.review_id == review_id:
                        return copy.deepcopy(review)
        return None

    async def list_transitions(self, story_id: UUID) -> Sequence[StateTransition]:
        async with self._lock:
            if story_id not in self._stories:
                raise StoryNotFoundError(story_id)
            return copy.deepcopy(self._transitions.get(story_id, []))

    async def classification_metrics(self) -> ClassificationMetrics:
        async with self._lock:
            reviewed = [
                review
                for reviews in self._reviews.values()
                for review in reviews
                if review.classification_accepted is not None
            ]
            accepted = sum(review.classification_accepted is True for review in reviewed)
        return ClassificationMetrics(
            reviewed_count=len(reviewed),
            accepted_count=accepted,
            accuracy=(accepted / len(reviewed) if reviewed else None),
        )

    async def healthcheck(self) -> None:
        return None


class InMemoryMemoryProvider:
    def __init__(self) -> None:
        self.writes: list[MemoryWrite] = []

    async def healthcheck(self) -> None:
        return None

    async def recall(self, query: MemoryQuery) -> Sequence[MemoryItem]:
        terms = {term.casefold() for term in query.query.split()}
        scored: list[MemoryItem] = []
        for index, memory in enumerate(self.writes):
            if not memory.approved:
                continue
            words = {term.casefold() for term in memory.content.split()}
            overlap = len(terms & words)
            score = min(1.0, overlap / max(1, len(terms)))
            scored.append(
                MemoryItem(
                    memory_id=f"mem-{index}",
                    content=memory.content,
                    score=score,
                    category=memory.category,
                    approved=memory.approved,
                )
            )
        return sorted(scored, key=lambda item: item.score, reverse=True)[: query.limit]

    async def remember(self, memory: MemoryWrite) -> None:
        self.writes.append(memory)

    async def aclose(self) -> None:
        return None


class DeterministicTextGenerator:
    def __init__(self) -> None:
        self.translation_calls = 0
        self.translation_response_cache_policies: list[bool] = []
        self.script_calls = 0
        self.spoken_regeneration_calls = 0

    async def healthcheck(self) -> None:
        return None

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
    ) -> TranslationResult:
        del story_id, memories
        self.translation_calls += 1
        self.translation_response_cache_policies.append(response_cache)
        normalized = " ".join(content.split())
        chinese_source = should_preserve_chinese_source(content, source_language)
        lowered = normalized.casefold()
        if any(word in lowered for word in ("cat", "dog", "猫", "狗")):
            category = ContentCategory.CATS_DOGS
        elif any(word in lowered for word in ("video", "clip", "视频")):
            category = ContentCategory.SHORT_VIDEO
        elif any(word in lowered for word in ("forum", "reddit", "pikabu", "论坛")):
            category = ContentCategory.FORUM
        else:
            category = ContentCategory.KINDNESS
        return TranslationResult(
            source_language=source_language or ("zh" if chinese_source else "und"),
            target_language=target_language,
            translated_title=(
                source_title
                if chinese_source or same_language(source_language, target_language)
                else f"[offline translation] {source_title}"
            ),
            translated_text=content if chinese_source else f"[offline translation] {normalized}",
            summary=normalized[:240],
            key_points=[normalized[:120]],
            screening=EditorialScreening(
                model_category=category,
                category=category,
                model_candidate_recommendation=True,
                candidate_recommendation=True,
                confidence=1.0,
                rationale="Deterministic keyword fixture classification.",
                risk_flags=[],
            ),
        )

    async def create_script(
        self,
        *,
        story_id: UUID,
        translation: TranslationResult,
        preferences: ScriptPreferences,
        memories: Sequence[MemoryItem],
    ) -> ScriptDraft:
        del story_id, memories
        self.script_calls += 1
        return ScriptDraft(
            title="Offline deterministic script",
            spoken_language=preferences.spoken_language or translation.target_language,
            segments=[
                ScriptSegmentDraft(
                    spoken_text=translation.summary,
                    spoken_language=preferences.spoken_language or translation.target_language,
                    speaker_id=preferences.speaker_id,
                    emotion=preferences.emotion,
                    speed=preferences.speed,
                    pitch=preferences.pitch,
                    visual_hint="Source headline and key facts",
                )
            ],
        )

    async def regenerate_spoken_segment(
        self,
        *,
        story_id: UUID,
        segment_id: UUID,
        caption_text: str,
        caption_language: str,
        spoken_language: str,
        memories: Sequence[MemoryItem],
    ) -> RegeneratedSpokenSegment:
        del story_id, memories
        self.spoken_regeneration_calls += 1
        spoken_text = (
            caption_text
            if same_language(caption_language, spoken_language)
            else f"[offline {spoken_language}] {caption_text}"
        )
        return RegeneratedSpokenSegment(
            segment_id=segment_id,
            spoken_text=spoken_text,
            spoken_language=spoken_language,
        )

    async def aclose(self) -> None:
        return None


def _write_silence_wav(path: Path, duration_ms: int) -> None:
    sample_rate = 16_000
    frame_count = max(1, round(duration_ms / 1000 * sample_rate))
    path.parent.mkdir(parents=True, exist_ok=True)
    silence = struct.pack("<h", 0) * frame_count
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(silence)


class DeterministicSpeechSynthesizer:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir.resolve()
        self.calls = 0
        self.fail_next = False

    async def healthcheck(self) -> None:
        return None

    async def synthesize(self, story_id: UUID, script: ScriptDocument) -> AudioBundle:
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise TTSGenerationError("Injected offline TTS failure.", story_id)
        clips: list[AudioClip] = []
        for segment in script.segments:
            duration_ms = max(500, len(segment.spoken_text) * 40)
            path = (
                self._output_dir
                / str(story_id)
                / f"r{script.revision}"
                / f"{segment.sequence:03d}-{segment.segment_id}.wav"
            )
            await asyncio.to_thread(_write_silence_wav, path, duration_ms)
            payload = await asyncio.to_thread(path.read_bytes)
            clips.append(
                AudioClip(
                    segment_id=segment.segment_id,
                    path=path.relative_to(self._output_dir).as_posix(),
                    format=AudioFormat.WAV,
                    duration_ms=duration_ms,
                    sample_rate_hz=16_000,
                    channels=1,
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
        empty_hash = hashlib.sha256(b"offline").hexdigest()
        return AudioBundle(
            revision=script.revision,
            provider="deterministic-offline-tts",
            model_identity="silence-fixture-v1",
            synthesis=SynthesisMetadata(
                seed=0,
                reference_audio_sha256=empty_hash,
                runtime_config_sha256=empty_hash,
                gpt_weights_sha256=empty_hash,
                sovits_weights_sha256=empty_hash,
                prompt_language="und",
                text_language=script.spoken_language,
            ),
            clips=clips,
        )

    async def aclose(self) -> None:
        return None
