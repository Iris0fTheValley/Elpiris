from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from god_news.application.memory import MemoryCoordinator
from god_news.domain.enums import ReviewDecision, ReviewStage, StoryStatus
from god_news.domain.fsm import transition_story
from god_news.domain.models import (
    AudioBundle,
    ClassificationMetrics,
    FetchedDocument,
    FirstReviewSubmission,
    IngestRequest,
    MemoryWrite,
    PipelineFailure,
    ProductionManifest,
    RetranslateStoryRequest,
    ReviewRecord,
    ScriptDocument,
    ScriptPreferences,
    ScriptReviewSubmission,
    SecondReviewSubmission,
    SourceItemIngestRequest,
    StateTransition,
    Story,
    StoryUpdate,
    SynthesizeStoryRequest,
    TimelineSegment,
    TranslationResult,
    utc_now,
)
from god_news.domain.ports import Fetcher, SpeechSynthesizer, StoryRepository, TextGenerator
from god_news.errors import (
    ArtifactNotReadyError,
    ConcurrentWriteError,
    GodNewsError,
    IdempotencyConflictError,
    InvalidTransitionError,
    StoryInvariantError,
    TTSGenerationError,
    VoiceProfileResolutionError,
)
from god_news.logging import story_log_context
from god_news.sources.models import NormalizedSourceItem
from god_news.sources.protocols import SourceItemNormalizer
from god_news.voice_profiles import VoiceProfileResolver

logger = logging.getLogger(__name__)


class StoryWorkflow:
    def __init__(
        self,
        *,
        repository: StoryRepository,
        fetcher: Fetcher,
        generator: TextGenerator,
        memory: MemoryCoordinator,
        synthesizer: SpeechSynthesizer,
        source_normalizer: SourceItemNormalizer,
        voice_resolver: VoiceProfileResolver,
    ) -> None:
        self._repository = repository
        self._fetcher = fetcher
        self._generator = generator
        self._memory = memory
        self._synthesizer = synthesizer
        self._source_normalizer = source_normalizer
        self._voice_resolver = voice_resolver
        self._story_locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()

    async def ingest(self, request: IngestRequest, *, trace_id: UUID | None = None) -> Story:
        document = await self._fetcher.fetch(request.source)
        return await self._ingest_document(request, document, trace_id=trace_id)

    async def ingest_source_item(
        self,
        request: SourceItemIngestRequest,
        *,
        trace_id: UUID | None = None,
    ) -> Story:
        normalized = self._source_normalizer.normalize(request.item)
        document = FetchedDocument.from_normalized_source(normalized)
        return await self._ingest_document(
            request,
            document,
            trace_id=trace_id,
            provenance=normalized,
        )

    async def _ingest_document(
        self,
        request: IngestRequest | SourceItemIngestRequest,
        document: FetchedDocument,
        *,
        trace_id: UUID | None,
        provenance: NormalizedSourceItem | None = None,
    ) -> Story:
        preferences = ScriptPreferences(
            style=request.style,
            target_duration_seconds=request.target_duration_seconds,
            speaker_id=request.speaker_id,
            emotion=request.emotion,
            speed=request.speed,
            pitch=request.pitch,
        )
        story = Story(
            trace_id=trace_id or uuid4(),
            status=StoryStatus.FETCHED,
            title=document.source.title,
            source=document.source,
            provenance=provenance,
            original_text=document.content,
            target_language=request.target_language,
            preferences=preferences,
        )
        with story_log_context(story.story_id):
            logger.info("story fetched")
            story = await self._repository.create(story)
            async with self._lock_for(story.story_id):
                return await self._translate(story)

    async def get(self, story_id: UUID) -> Story:
        return await self._repository.get(story_id)

    async def list(
        self,
        *,
        status: StoryStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> Sequence[Story]:
        return await self._repository.list(
            status=status,
            limit=limit,
            offset=offset,
            include_archived=include_archived,
        )

    async def reviews(self, story_id: UUID) -> Sequence[ReviewRecord]:
        await self._repository.get(story_id)
        return await self._repository.list_reviews(story_id)

    async def transitions(self, story_id: UUID) -> Sequence[StateTransition]:
        await self._repository.get(story_id)
        return await self._repository.list_transitions(story_id)

    async def classification_metrics(self) -> ClassificationMetrics:
        return await self._repository.classification_metrics()

    async def resume(self, story_id: UUID) -> Story:
        async with self._lock_for(story_id):
            story = await self._repository.get(story_id)
            with story_log_context(story_id):
                if story.status is StoryStatus.FETCHED:
                    return await self._translate(story)
                if story.status is StoryStatus.TRANSLATED:
                    if story.translation is None:
                        raise ArtifactNotReadyError(story_id, "Translation is missing.")
                    pending = transition_story(story, StoryStatus.PENDING_FIRST_REVIEW)
                    return await self._repository.save(
                        pending,
                        expected_version=story.version,
                        transition_reason="translation recovery queued for first review",
                    )
                if story.status is StoryStatus.PROCESSING_SCRIPT:
                    return await self._create_script(story)
                if story.status is StoryStatus.PROCESSING_TTS:
                    return await self._complete_tts_from_processing(story)
                raise InvalidTransitionError(story_id, story.status, story.status)

    async def archive(self, story_id: UUID) -> Story:
        """Soft-delete a story while retaining evidence and audit history."""

        async with self._lock_for(story_id):
            story = await self._repository.get(story_id)
            if story.status is StoryStatus.ARCHIVED:
                raise InvalidTransitionError(story_id, story.status, StoryStatus.ARCHIVED)
            archived = transition_story(story, StoryStatus.ARCHIVED)
            return await self._repository.save(
                archived,
                expected_version=story.version,
                transition_reason="story archived",
            )

    async def retranslate(
        self,
        story_id: UUID,
        request: RetranslateStoryRequest,
    ) -> Story:
        """Regenerate a translation before first review without changing source evidence."""

        async with self._lock_for(story_id):
            story = await self._repository.get(story_id)
            if request.expected_story_version != story.version:
                raise ConcurrentWriteError(story_id)
            if story.status is not StoryStatus.PENDING_FIRST_REVIEW:
                raise InvalidTransitionError(
                    story_id,
                    story.status,
                    StoryStatus.PENDING_FIRST_REVIEW,
                )
            with story_log_context(story_id):
                try:
                    memories = await self._memory.recall(
                        f"editorial context for {story.source.title}: {story.original_text[:500]}"
                    )
                    translation = await self._generator.translate_and_summarize(
                        story_id=story.story_id,
                        content=story.original_text,
                        source_language=story.source.detected_language,
                        target_language=story.target_language,
                        memories=memories,
                    )
                    updated = story.model_copy(
                        update={
                            "translation": translation,
                            "last_failure": None,
                            "updated_at": utc_now(),
                        }
                    )
                    saved = await self._repository.save(
                        updated,
                        expected_version=story.version,
                    )
                    logger.info("translation regenerated before first review")
                    return saved
                except Exception as exc:
                    await self._store_failure(story, exc)
                    raise

    async def reopen(self, story_id: UUID) -> Story:
        """Return a completed story to the final-review gate for another pass."""

        async with self._lock_for(story_id):
            story = await self._repository.get(story_id)
            if story.status is not StoryStatus.DONE:
                raise InvalidTransitionError(
                    story_id,
                    story.status,
                    StoryStatus.PENDING_SECOND_REVIEW,
                )
            reopened = transition_story(story, StoryStatus.PENDING_SECOND_REVIEW)
            return await self._repository.save(
                reopened,
                expected_version=story.version,
                transition_reason="story reopened for final review",
            )

    async def update(self, story_id: UUID, request: StoryUpdate) -> Story:
        """Apply an optimistic-concurrency edit without touching source evidence.

        Style and duration are input to script generation.  Once a script exists,
        changing either would make approved artifacts misleading, so callers must
        revise the script through the script-review flow instead.
        """

        async with self._lock_for(story_id):
            story = await self._repository.get(story_id)
            if story.status is StoryStatus.ARCHIVED:
                raise InvalidTransitionError(story_id, story.status, story.status)
            if request.expected_story_version != story.version:
                raise ConcurrentWriteError(story_id)

            changes_preferences = (
                request.style is not None or request.target_duration_seconds is not None
            )
            if changes_preferences and story.script is not None:
                raise StoryInvariantError(
                    story_id,
                    "Style and target duration cannot change after script generation. "
                    "Submit a revised script during final review instead.",
                )

            preferences = story.preferences.model_copy(
                update={
                    "style": request.style
                    if request.style is not None
                    else story.preferences.style,
                    "target_duration_seconds": request.target_duration_seconds
                    if request.target_duration_seconds is not None
                    else story.preferences.target_duration_seconds,
                }
            )
            updated = story.model_copy(
                update={
                    "title": request.title if request.title is not None else story.title,
                    "preferences": preferences,
                    "updated_at": utc_now(),
                }
            )
            return await self._repository.save(
                updated,
                expected_version=story.version,
            )

    async def submit_first_review(
        self,
        story_id: UUID,
        submission: FirstReviewSubmission,
    ) -> Story:
        async with self._lock_for(story_id):
            existing = await self._repository.get_review(submission.review_id)
            if existing is not None:
                if (
                    existing.story_id != story_id
                    or existing.stage is not ReviewStage.FIRST
                    or existing.request_sha256 != self._review_request_hash(submission)
                ):
                    raise IdempotencyConflictError(story_id)
                return await self._repository.get(story_id)
            story = await self._repository.get(story_id)
            with story_log_context(story_id):
                return await self._submit_first_review_locked(story, submission)

    async def _submit_first_review_locked(
        self,
        story: Story,
        submission: FirstReviewSubmission,
    ) -> Story:
        story_id = story.story_id
        if submission.expected_story_version != story.version:
            raise ConcurrentWriteError(story_id)
        if story.status is not StoryStatus.PENDING_FIRST_REVIEW:
            raise InvalidTransitionError(
                story_id,
                story.status,
                StoryStatus.PROCESSING_SCRIPT,
            )
        if story.translation is None:
            raise ArtifactNotReadyError(story_id, "Translation is missing.")
        corrected = self._apply_first_review_edits(story, submission)
        review = self._build_review(
            corrected,
            review_id=submission.review_id,
            stage=ReviewStage.FIRST,
            decision=submission.decision,
            reviewer_id=submission.reviewer_id,
            note=submission.note,
            request_sha256=self._review_request_hash(submission),
        )
        if submission.decision is ReviewDecision.REQUEST_CHANGES:
            corrected = corrected.model_copy(update={"updated_at": utc_now()})
            corrected = await self._repository.save(
                corrected,
                expected_version=story.version,
                review=review,
            )
            await self._remember_review(corrected, review, approved=False)
            return corrected

        await self._require_enabled_tts_voice(story_id, corrected.preferences.speaker_id)
        processing = transition_story(corrected, StoryStatus.PROCESSING_SCRIPT)
        processing = await self._repository.save(
            processing,
            expected_version=story.version,
            transition_reason="first review approved",
            review=review,
        )
        logger.info("first review approved")
        await self._remember_translation(processing, approved=True)
        await self._remember_review(processing, review, approved=True)
        return await self._create_script(processing)

    async def submit_script_review(
        self,
        story_id: UUID,
        submission: ScriptReviewSubmission,
    ) -> Story:
        """Record the human script gate before any expensive local TTS work."""

        async with self._lock_for(story_id):
            existing = await self._repository.get_review(submission.review_id)
            if existing is not None:
                if (
                    existing.story_id != story_id
                    or existing.stage is not ReviewStage.SCRIPT
                    or existing.request_sha256 != self._review_request_hash(submission)
                ):
                    raise IdempotencyConflictError(story_id)
                return await self._repository.get(story_id)
            story = await self._repository.get(story_id)
            with story_log_context(story_id):
                return await self._submit_script_review_locked(story, submission)

    async def _submit_script_review_locked(
        self,
        story: Story,
        submission: ScriptReviewSubmission,
    ) -> Story:
        story_id = story.story_id
        if submission.expected_story_version != story.version:
            raise ConcurrentWriteError(story_id)
        if story.status is not StoryStatus.SCRIPT_READY:
            raise InvalidTransitionError(story_id, story.status, StoryStatus.PENDING_TTS)
        if story.script is None:
            raise ArtifactNotReadyError(story_id, "Script is missing.")

        if submission.decision is ReviewDecision.REQUEST_CHANGES:
            reviewed_story = story
            if submission.revised_script is not None:
                revised_script = submission.revised_script.model_copy(
                    update={"revision": story.script.revision + 1}
                )
                reviewed_story = story.model_copy(
                    update={
                        "script": revised_script,
                        "audio": None,
                        "last_failure": None,
                        "updated_at": utc_now(),
                    }
                )
            review = self._build_review(
                reviewed_story,
                review_id=submission.review_id,
                stage=ReviewStage.SCRIPT,
                decision=submission.decision,
                reviewer_id=submission.reviewer_id,
                note=submission.note,
                request_sha256=self._review_request_hash(submission),
            )
            reviewed_story = reviewed_story.model_copy(update={"updated_at": utc_now()})
            reviewed_story = await self._repository.save(
                reviewed_story,
                expected_version=story.version,
                review=review,
            )
            await self._remember_review(reviewed_story, review, approved=False)
            return reviewed_story

        review = self._build_review(
            story,
            review_id=submission.review_id,
            stage=ReviewStage.SCRIPT,
            decision=submission.decision,
            reviewer_id=submission.reviewer_id,
            note=submission.note,
            request_sha256=self._review_request_hash(submission),
        )
        pending = transition_story(story, StoryStatus.PENDING_TTS)
        pending = await self._repository.save(
            pending,
            expected_version=story.version,
            transition_reason="script review approved; waiting for manual TTS synthesis",
            review=review,
        )
        logger.info("script review approved; awaiting manual TTS")
        await self._remember_review(pending, review, approved=True)
        return pending

    async def synthesize(self, story_id: UUID, request: SynthesizeStoryRequest) -> Story:
        """Explicit command boundary for isolated local TTS synthesis.

        This command intentionally is not part of ``resume`` for script-ready
        stories: an editor must approve a concrete script and deliberately
        start the high-cost local process.
        """

        async with self._lock_for(story_id):
            story = await self._repository.get(story_id)
            if request.expected_story_version != story.version:
                raise ConcurrentWriteError(story_id)
            if story.status is not StoryStatus.PENDING_TTS:
                raise InvalidTransitionError(story_id, story.status, StoryStatus.PROCESSING_TTS)
            if story.script is None:
                raise ArtifactNotReadyError(story_id, "Script is missing.")
            with story_log_context(story_id):
                processing = transition_story(story, StoryStatus.PROCESSING_TTS)
                processing = await self._repository.save(
                    processing,
                    expected_version=story.version,
                    transition_reason="manual TTS synthesis started",
                )
                return await self._complete_tts_from_processing(processing)

    async def submit_second_review(
        self,
        story_id: UUID,
        submission: SecondReviewSubmission,
    ) -> Story:
        async with self._lock_for(story_id):
            existing = await self._repository.get_review(submission.review_id)
            if existing is not None:
                if (
                    existing.story_id != story_id
                    or existing.stage is not ReviewStage.SECOND
                    or existing.request_sha256 != self._review_request_hash(submission)
                ):
                    raise IdempotencyConflictError(story_id)
                return await self._repository.get(story_id)
            story = await self._repository.get(story_id)
            with story_log_context(story_id):
                return await self._submit_second_review_locked(story, submission)

    async def _submit_second_review_locked(
        self,
        story: Story,
        submission: SecondReviewSubmission,
    ) -> Story:
        story_id = story.story_id
        if submission.expected_story_version != story.version:
            raise ConcurrentWriteError(story_id)
        if story.status is not StoryStatus.PENDING_SECOND_REVIEW:
            raise InvalidTransitionError(story_id, story.status, StoryStatus.DONE)
        if submission.decision is ReviewDecision.REQUEST_CHANGES:
            if submission.revised_script is None:
                review = self._build_review(
                    story,
                    review_id=submission.review_id,
                    stage=ReviewStage.SECOND,
                    decision=submission.decision,
                    reviewer_id=submission.reviewer_id,
                    note=submission.note,
                    request_sha256=self._review_request_hash(submission),
                )
                unchanged = story.model_copy(update={"updated_at": utc_now()})
                unchanged = await self._repository.save(
                    unchanged,
                    expected_version=story.version,
                    review=review,
                )
                await self._remember_review(unchanged, review, approved=False)
                return unchanged
            if story.script is None:
                raise ArtifactNotReadyError(story_id, "Script is missing.")
            next_revision = story.script.revision + 1
            revised_script = submission.revised_script.model_copy(
                update={"revision": next_revision}
            )
            revised = story.model_copy(
                update={
                    "script": revised_script,
                    "audio": None,
                    "last_failure": None,
                    "updated_at": utc_now(),
                }
            )
            review = self._build_review(
                revised,
                review_id=submission.review_id,
                stage=ReviewStage.SECOND,
                decision=submission.decision,
                reviewer_id=submission.reviewer_id,
                note=submission.note,
                request_sha256=self._review_request_hash(submission),
            )
            script_ready = transition_story(revised, StoryStatus.SCRIPT_READY)
            script_ready = await self._repository.save(
                script_ready,
                expected_version=story.version,
                transition_reason="final review returned revised script to script review",
                review=review,
            )
            await self._remember_review(script_ready, review, approved=False)
            return script_ready

        if story.script is None or story.audio is None:
            raise ArtifactNotReadyError(
                story_id,
                "Script and audio must both be ready before final approval.",
            )
        review = self._build_review(
            story,
            review_id=submission.review_id,
            stage=ReviewStage.SECOND,
            decision=submission.decision,
            reviewer_id=submission.reviewer_id,
            note=submission.note,
            request_sha256=self._review_request_hash(submission),
        )
        done = transition_story(story, StoryStatus.DONE)
        done = await self._repository.save(
            done,
            expected_version=story.version,
            transition_reason="second review approved",
            review=review,
        )
        logger.info("second review approved; story done")
        await self._remember_script(done, approved=True)
        await self._remember_review(done, review, approved=True)
        return done

    async def production_manifest(self, story_id: UUID) -> ProductionManifest:
        story = await self._repository.get(story_id)
        if story.status is not StoryStatus.DONE:
            raise ArtifactNotReadyError(story_id, "Final review is not approved.")
        if story.script is None or story.audio is None:
            raise ArtifactNotReadyError(story_id, "Script or audio is not ready.")
        clips = {clip.segment_id: clip for clip in story.audio.clips}
        if set(clips) != {segment.segment_id for segment in story.script.segments}:
            raise ArtifactNotReadyError(story_id, "Script and audio segment sets do not match.")
        cursor = 0
        timeline: list[TimelineSegment] = []
        for segment in story.script.segments:
            clip = clips[segment.segment_id]
            end = cursor + clip.duration_ms
            timeline.append(
                TimelineSegment(
                    segment_id=segment.segment_id,
                    sequence=segment.sequence,
                    start_ms=cursor,
                    end_ms=end,
                    spoken_text=segment.spoken_text,
                    spoken_language=segment.spoken_language,
                    captions=segment.captions,
                    speaker_id=segment.speaker_id,
                    emotion=segment.emotion,
                    scene_transition=segment.scene_transition,
                    visual_hint=segment.visual_hint,
                    audio_path=clip.path,
                )
            )
            cursor = end
        return ProductionManifest(
            story_id=story_id,
            script_revision=story.script.revision,
            spoken_language=story.script.spoken_language,
            total_duration_ms=cursor,
            timeline=timeline,
        )

    async def _translate(self, story: Story) -> Story:
        checkpoint = story
        try:
            memories = await self._memory.recall(
                f"editorial context for {story.source.title}: {story.original_text[:500]}"
            )
            translation = await self._generator.translate_and_summarize(
                story_id=story.story_id,
                content=story.original_text,
                source_language=story.source.detected_language,
                target_language=story.target_language,
                memories=memories,
            )
            translated = transition_story(
                story,
                StoryStatus.TRANSLATED,
                translation=translation,
            )
            translated = await self._repository.save(
                translated,
                expected_version=story.version,
                transition_reason="translation completed",
            )
            checkpoint = translated
            pending = transition_story(translated, StoryStatus.PENDING_FIRST_REVIEW)
            pending = await self._repository.save(
                pending,
                expected_version=translated.version,
                transition_reason="translation queued for first review",
            )
            logger.info("translation ready for first review")
            return pending
        except Exception as exc:
            await self._store_failure(checkpoint, exc)
            raise

    async def _create_script(self, story: Story) -> Story:
        """Generate a draft script and stop at the explicit script-review gate."""

        if story.translation is None:
            raise ArtifactNotReadyError(story.story_id, "Translation is missing.")
        try:
            memories = await self._memory.recall(
                f"script style and approved context for: {story.translation.summary}"
            )
            draft = await self._generator.create_script(
                story_id=story.story_id,
                translation=story.translation,
                preferences=story.preferences,
                memories=memories,
            )
            script = ScriptDocument.from_draft(draft)
            ready = transition_story(
                story,
                StoryStatus.SCRIPT_READY,
                script=script,
            )
            ready = await self._repository.save(
                ready,
                expected_version=story.version,
                transition_reason="script generated",
            )
        except Exception as exc:
            await self._store_failure(story, exc)
            raise
        logger.info("script ready for human script review")
        return ready

    def _lock_for(self, story_id: UUID) -> asyncio.Lock:
        lock = self._story_locks.get(story_id)
        if lock is None:
            lock = asyncio.Lock()
            self._story_locks[story_id] = lock
        return lock

    async def _complete_tts_from_processing(self, story: Story) -> Story:
        """Run TTS from its durable processing checkpoint.

        A failure deliberately returns the story to ``PENDING_TTS`` with a
        persisted failure record.  That leaves the approved script immutable
        and makes each retry an explicit, auditable command.
        """

        if story.status is not StoryStatus.PROCESSING_TTS:
            raise InvalidTransitionError(
                story.story_id,
                story.status,
                StoryStatus.PENDING_SECOND_REVIEW,
            )
        if story.script is None:
            raise ArtifactNotReadyError(story.story_id, "Script is missing.")
        try:
            audio = await self._synthesizer.synthesize(story.story_id, story.script)
            self._validate_audio(story, story.script, audio)
            pending = transition_story(
                story,
                StoryStatus.PENDING_SECOND_REVIEW,
                audio=audio,
            )
            pending = await self._repository.save(
                pending,
                expected_version=story.version,
                transition_reason="manual TTS synthesis completed; queued for final review",
            )
            logger.info("audio ready for final review")
            return pending
        except asyncio.CancelledError:
            await asyncio.shield(
                self._return_tts_to_pending(
                    story,
                    TTSGenerationError(
                        "Local TTS synthesis was interrupted; manually retry when ready.",
                        story.story_id,
                    ),
                )
            )
            raise
        except Exception as exc:
            await self._return_tts_to_pending(story, exc)
            raise

    async def _return_tts_to_pending(self, story: Story, exc: Exception) -> None:
        """Durably expose a failed manual TTS attempt as a safe retry state."""

        try:
            pending = transition_story(story, StoryStatus.PENDING_TTS)
            await self._store_failure(
                pending,
                exc,
                transition_reason="TTS synthesis failed; manual retry required",
            )
        except Exception:
            logger.exception("could not return failed TTS synthesis to PENDING_TTS")

    def _apply_first_review_edits(
        self,
        story: Story,
        submission: FirstReviewSubmission,
    ) -> Story:
        assert story.translation is not None
        current_screening = story.translation.screening
        screening = current_screening.model_copy(
            update={
                "category": submission.corrected_category or current_screening.category,
                "candidate_recommendation": (
                    submission.corrected_candidate_recommendation
                    if submission.corrected_candidate_recommendation is not None
                    else current_screening.candidate_recommendation
                ),
            }
        )
        translation: TranslationResult = story.translation.model_copy(
            update={
                "translated_text": (
                    submission.corrected_translation or story.translation.translated_text
                ),
                "summary": submission.corrected_summary or story.translation.summary,
                "key_points": (
                    submission.corrected_key_points
                    if submission.corrected_key_points is not None
                    else story.translation.key_points
                ),
                "screening": screening,
            }
        )
        translation_was_corrected = any(
            value is not None
            for value in (
                submission.corrected_translation,
                submission.corrected_summary,
                submission.corrected_key_points,
            )
        )
        return story.model_copy(
            update={
                "translation": translation,
                "preferences": submission.preferences or story.preferences,
                "last_failure": None if translation_was_corrected else story.last_failure,
            }
        )

    async def _require_enabled_tts_voice(self, story_id: UUID, speaker_id: str) -> None:
        """Fail closed before script generation if the chosen narrator is not usable.

        This is intentionally a narrow port call.  The workflow does not know
        how profiles are stored; it only requires a resolver to attest that the
        selected speaker is currently enabled and has complete local-TTS assets.
        """

        try:
            profile = await self._voice_resolver.resolve_voice(speaker_id)
        except Exception as exc:
            logger.exception("could not verify narrator role before first-review approval")
            raise VoiceProfileResolutionError(story_id) from exc
        if profile is None:
            raise StoryInvariantError(
                story_id,
                "Choose an enabled role with a complete local TTS configuration "
                "before approving first review.",
            )

    @staticmethod
    def _validate_audio(story: Story, script: ScriptDocument, audio: AudioBundle) -> None:
        script_ids = {segment.segment_id for segment in script.segments}
        audio_ids = {clip.segment_id for clip in audio.clips}
        if audio.revision != script.revision or audio_ids != script_ids:
            raise TTSGenerationError(
                "Speech provider returned audio that does not match the script revision.",
                story.story_id,
            )

    @staticmethod
    def _build_review(
        story: Story,
        *,
        review_id: UUID,
        stage: ReviewStage,
        decision: ReviewDecision,
        reviewer_id: str,
        note: str | None,
        request_sha256: str,
    ) -> ReviewRecord:
        if story.translation is None:
            raise ArtifactNotReadyError(story.story_id, "Translation is missing.")
        translation_hash = hashlib.sha256(
            story.translation.model_dump_json().encode("utf-8")
        ).hexdigest()
        audio_hash = (
            hashlib.sha256(story.audio.model_dump_json().encode("utf-8")).hexdigest()
            if story.audio is not None
            else None
        )
        screening = story.translation.screening
        ai_category = screening.model_category if stage is ReviewStage.FIRST else None
        human_category = screening.category if stage is ReviewStage.FIRST else None
        return ReviewRecord(
            review_id=review_id,
            story_id=story.story_id,
            stage=stage,
            decision=decision,
            reviewer_id=reviewer_id,
            note=note,
            reviewed_story_version=story.version,
            translation_sha256=translation_hash,
            script_revision=story.script.revision if story.script else None,
            audio_sha256=audio_hash,
            request_sha256=request_sha256,
            ai_category=ai_category,
            human_category=human_category,
            classification_accepted=(
                ai_category == human_category if stage is ReviewStage.FIRST else None
            ),
        )

    @staticmethod
    def _review_request_hash(
        submission: FirstReviewSubmission | ScriptReviewSubmission | SecondReviewSubmission,
    ) -> str:
        return hashlib.sha256(submission.model_dump_json().encode("utf-8")).hexdigest()

    async def _store_failure(
        self,
        story: Story,
        exc: Exception,
        *,
        transition_reason: str | None = None,
    ) -> None:
        code = exc.code if isinstance(exc, GodNewsError) else "internal_error"
        message = (
            exc.public_message
            if isinstance(exc, GodNewsError)
            else "An internal pipeline operation failed."
        )
        retryable = exc.retryable if isinstance(exc, GodNewsError) else False
        failed = story.model_copy(
            update={
                "last_failure": PipelineFailure(
                    code=code,
                    message=message,
                    retryable=retryable,
                ),
                "updated_at": utc_now(),
            }
        )
        try:
            await self._repository.save(
                failed,
                expected_version=story.version,
                transition_reason=transition_reason,
            )
        except Exception:
            logger.exception("could not persist pipeline failure")

    async def _remember_translation(self, story: Story, *, approved: bool) -> None:
        if story.translation is None:
            return
        await self._memory.remember(
            MemoryWrite(
                content=(
                    f"Story summary ({story.target_language}): {story.translation.summary}\n"
                    f"Key points: {'; '.join(story.translation.key_points)}"
                ),
                category="approved_translation" if approved else "draft_translation",
                story_id=story.story_id,
                approved=approved,
                source_hash=story.source.content_sha256,
            )
        )

    async def _remember_script(self, story: Story, *, approved: bool) -> None:
        if story.script is None:
            return
        text = "\n".join(segment.spoken_text for segment in story.script.segments)
        await self._memory.remember(
            MemoryWrite(
                content=f"Editorial script revision {story.script.revision}: {text}",
                category="approved_script" if approved else "draft_script",
                story_id=story.story_id,
                approved=approved,
                source_hash=story.source.content_sha256,
            )
        )

    async def _remember_review(
        self,
        story: Story,
        review: ReviewRecord,
        *,
        approved: bool,
    ) -> None:
        if not review.note:
            return
        await self._memory.remember(
            MemoryWrite(
                content=f"Human {review.stage.value} review feedback: {review.note}",
                category="human_review",
                story_id=story.story_id,
                approved=approved,
                source_hash=story.source.content_sha256,
            )
        )
