from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from god_news.domain.enums import ContentCategory, ReviewDecision, StoryStatus
from god_news.domain.fsm import transition_story
from god_news.domain.models import (
    FirstReviewSubmission,
    IngestRequest,
    PipelineFailure,
    RetranslateStoryRequest,
    ScriptReviewSubmission,
    SecondReviewSubmission,
    SynthesizeStoryRequest,
    TextSource,
)
from god_news.errors import (
    ArtifactNotReadyError,
    ConcurrentWriteError,
    IdempotencyConflictError,
    InvalidTransitionError,
    StoryInvariantError,
    TTSGenerationError,
)

from .conftest import Stack


def ingest_request() -> IngestRequest:
    return IngestRequest(
        source=TextSource(
            title="International fixture",
            language="en",
            text=(
                "The city council approved a renewable energy pilot on Tuesday. "
                "Officials said the twelve-month trial will publish monthly results."
            ),
        ),
        target_language="zh-CN",
        style="accurate narration",
        target_duration_seconds=60,
    )


async def _approve_first_review(stack: Stack, story_id, version: int):  # type: ignore[no-untyped-def]
    return await stack.workflow.submit_first_review(
        story_id,
        FirstReviewSubmission(
            expected_story_version=version,
            decision=ReviewDecision.APPROVE,
            reviewer_id="editor-1",
            note="Names and quantities checked.",
        ),
    )


async def _approve_script_review(stack: Stack, story_id, version: int):  # type: ignore[no-untyped-def]
    return await stack.workflow.submit_script_review(
        story_id,
        ScriptReviewSubmission(
            expected_story_version=version,
            decision=ReviewDecision.APPROVE,
            reviewer_id="script-editor",
            note="Narration is ready for synthesis.",
        ),
    )


@pytest.mark.asyncio
async def test_complete_review_gated_pipeline(stack: Stack) -> None:
    story = await stack.workflow.ingest(ingest_request())
    assert story.status is StoryStatus.PENDING_FIRST_REVIEW
    assert story.source.title == "International fixture"
    assert story.translation is not None
    assert story.translation.translated_title == "[offline translation] International fixture"
    assert story.title == story.translation.translated_title
    assert stack.synthesizer.calls == 0
    assert stack.generator.script_calls == 0
    assert stack.memory.writes == []

    with pytest.raises(ArtifactNotReadyError):
        await stack.workflow.production_manifest(story.story_id)

    story = await _approve_first_review(stack, story.story_id, story.version)
    assert story.status is StoryStatus.SCRIPT_READY
    assert story.script is not None
    assert story.audio is None
    assert stack.generator.script_calls == 1
    assert stack.synthesizer.calls == 0

    story = await _approve_script_review(stack, story.story_id, story.version)
    assert story.status is StoryStatus.PENDING_TTS
    assert story.audio is None
    assert stack.synthesizer.calls == 0

    story = await stack.workflow.synthesize(
        story.story_id,
        SynthesizeStoryRequest(expected_story_version=story.version),
    )
    assert story.status is StoryStatus.PENDING_SECOND_REVIEW
    assert story.audio is not None
    assert stack.synthesizer.calls == 1

    story = await stack.workflow.submit_second_review(
        story.story_id,
        SecondReviewSubmission(
            expected_story_version=story.version,
            decision=ReviewDecision.APPROVE,
            reviewer_id="editor-2",
            note="Audio and script approved.",
        ),
    )
    assert story.status is StoryStatus.DONE

    manifest = await stack.workflow.production_manifest(story.story_id)
    assert manifest.total_duration_ms > 0
    assert len(manifest.timeline) == len(story.script.segments)  # type: ignore[union-attr]
    assert manifest.timeline[0].scene_transition.value == "black"

    transitions = await stack.workflow.transitions(story.story_id)
    assert [(item.from_status, item.to_status) for item in transitions] == [
        (StoryStatus.FETCHED, StoryStatus.TRANSLATED),
        (StoryStatus.TRANSLATED, StoryStatus.PENDING_FIRST_REVIEW),
        (StoryStatus.PENDING_FIRST_REVIEW, StoryStatus.PROCESSING_SCRIPT),
        (StoryStatus.PROCESSING_SCRIPT, StoryStatus.SCRIPT_READY),
        (StoryStatus.SCRIPT_READY, StoryStatus.PENDING_TTS),
        (StoryStatus.PENDING_TTS, StoryStatus.PROCESSING_TTS),
        (StoryStatus.PROCESSING_TTS, StoryStatus.PENDING_SECOND_REVIEW),
        (StoryStatus.PENDING_SECOND_REVIEW, StoryStatus.DONE),
    ]
    assert len(await stack.workflow.reviews(story.story_id)) == 3
    assert stack.memory.writes
    assert all(memory.approved for memory in stack.memory.writes)


@pytest.mark.asyncio
async def test_first_review_changes_do_not_start_expensive_work(stack: Stack) -> None:
    story = await stack.workflow.ingest(ingest_request())
    story = await stack.repository.save(
        story.model_copy(
            update={
                "last_failure": PipelineFailure(
                    code="llm_generation_failed",
                    message="The previous automatic translation was invalid.",
                    retryable=True,
                )
            }
        ),
        expected_version=story.version,
    )
    story = await stack.workflow.submit_first_review(
        story.story_id,
        FirstReviewSubmission(
            expected_story_version=story.version,
            decision=ReviewDecision.REQUEST_CHANGES,
            reviewer_id="editor-1",
            note="Use a more precise summary.",
            corrected_title="城市通过可再生能源试点",
            corrected_summary="The council approved a twelve-month renewable energy pilot.",
            corrected_key_points=["Twelve-month pilot", "Monthly results"],
            corrected_category=ContentCategory.FORUM,
            corrected_candidate_recommendation=False,
        ),
    )
    assert story.status is StoryStatus.PENDING_FIRST_REVIEW
    assert story.translation is not None
    assert story.title == "城市通过可再生能源试点"
    assert story.translation.translated_title == "城市通过可再生能源试点"
    assert story.translation.summary.startswith("The council approved")
    assert story.translation.key_points == ["Twelve-month pilot", "Monthly results"]
    assert story.translation.screening.model_category is ContentCategory.KINDNESS
    assert story.translation.screening.category is ContentCategory.FORUM
    assert story.translation.screening.candidate_recommendation is False
    assert story.last_failure is None
    assert stack.synthesizer.calls == 0
    assert stack.generator.script_calls == 0
    review = (await stack.workflow.reviews(story.story_id))[0]
    assert review.ai_category is ContentCategory.KINDNESS
    assert review.human_category is ContentCategory.FORUM
    assert review.classification_accepted is False
    metrics = await stack.workflow.classification_metrics()
    assert metrics.reviewed_count == 1
    assert metrics.accepted_count == 0
    assert metrics.accuracy == 0.0


@pytest.mark.asyncio
async def test_unreviewed_translation_can_be_regenerated_with_version_guard(stack: Stack) -> None:
    story = await stack.workflow.ingest(ingest_request())
    original_version = story.version

    regenerated = await stack.workflow.retranslate(
        story.story_id,
        RetranslateStoryRequest(expected_story_version=story.version),
    )

    assert regenerated.status is StoryStatus.PENDING_FIRST_REVIEW
    assert regenerated.version == original_version + 1
    assert regenerated.source == story.source
    assert regenerated.original_text == story.original_text
    assert regenerated.translation is not None
    assert stack.generator.translation_calls == 2
    assert stack.generator.translation_response_cache_policies == [True, False]

    with pytest.raises(ConcurrentWriteError):
        await stack.workflow.retranslate(
            story.story_id,
            RetranslateStoryRequest(expected_story_version=story.version),
        )


@pytest.mark.asyncio
async def test_first_review_rejects_an_unconfigured_tts_speaker_before_script_generation(
    stack: Stack,
) -> None:
    story = await stack.workflow.ingest(ingest_request())

    with pytest.raises(StoryInvariantError, match="enabled role"):
        await stack.workflow.submit_first_review(
            story.story_id,
            FirstReviewSubmission(
                expected_story_version=story.version,
                decision=ReviewDecision.APPROVE,
                reviewer_id="editor-1",
                preferences=story.preferences.model_copy(
                    update={"speaker_id": "unconfigured-speaker"}
                ),
            ),
        )

    unchanged = await stack.workflow.get(story.story_id)
    assert unchanged.status is StoryStatus.PENDING_FIRST_REVIEW
    assert await stack.workflow.reviews(story.story_id) == []
    assert stack.generator.script_calls == 0
    assert stack.synthesizer.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_language", "content", "expected"),
    [
        ("zh-CN", "  这是一段保留空白的中文原文。  ", "  这是一段保留空白的中文原文。  "),
        (
            None,
            "  这是一段无语言标记但有足够汉字的新闻原文。  ",
            "  这是一段无语言标记但有足够汉字的新闻原文。  ",
        ),
        (
            "en",
            "这是一段显式英文来源的中文引文。",
            "[offline translation] 这是一段显式英文来源的中文引文。",
        ),
    ],
)
async def test_deterministic_generator_matches_chinese_translation_contract(
    stack: Stack,
    source_language: str | None,
    content: str,
    expected: str,
) -> None:
    result = await stack.generator.translate_and_summarize(
        story_id=uuid4(),
        source_title="语言契约标题",
        content=content,
        source_language=source_language,
        target_language="en",
        memories=[],
    )
    assert result.translated_text == expected
    assert result.summary
    assert result.screening.category is not None


@pytest.mark.asyncio
async def test_tts_failure_returns_to_manual_retry_gate(stack: Stack) -> None:
    story = await stack.workflow.ingest(ingest_request())
    story = await _approve_first_review(stack, story.story_id, story.version)
    with pytest.raises(InvalidTransitionError):
        await stack.workflow.resume(story.story_id)
    story = await _approve_script_review(stack, story.story_id, story.version)
    stack.synthesizer.fail_next = True

    with pytest.raises(TTSGenerationError):
        await stack.workflow.synthesize(
            story.story_id,
            SynthesizeStoryRequest(expected_story_version=story.version),
        )
    failed = await stack.workflow.get(story.story_id)
    assert failed.status is StoryStatus.PENDING_TTS
    assert failed.last_failure is not None

    with pytest.raises(InvalidTransitionError):
        await stack.workflow.resume(failed.story_id)

    retried = await stack.workflow.synthesize(
        failed.story_id,
        SynthesizeStoryRequest(expected_story_version=failed.version),
    )
    assert retried.status is StoryStatus.PENDING_SECOND_REVIEW
    assert retried.last_failure is None
    assert stack.synthesizer.calls == 2


@pytest.mark.asyncio
async def test_resume_recovers_only_the_durable_tts_processing_checkpoint(stack: Stack) -> None:
    story = await stack.workflow.ingest(ingest_request())
    story = await _approve_first_review(stack, story.story_id, story.version)
    pending = await _approve_script_review(stack, story.story_id, story.version)
    processing = transition_story(pending, StoryStatus.PROCESSING_TTS)
    processing = await stack.repository.save(
        processing,
        expected_version=pending.version,
        transition_reason="fixture interrupted after manual TTS started",
    )

    recovered = await stack.workflow.resume(processing.story_id)
    assert recovered.status is StoryStatus.PENDING_SECOND_REVIEW
    assert stack.synthesizer.calls == 1


@pytest.mark.asyncio
async def test_review_idempotency_and_manual_tts_lock_prevent_duplicate_work(stack: Stack) -> None:
    story = await stack.workflow.ingest(ingest_request())
    submission = FirstReviewSubmission(
        expected_story_version=story.version,
        decision=ReviewDecision.APPROVE,
        reviewer_id="editor-1",
    )
    results = await asyncio.gather(
        stack.workflow.submit_first_review(story.story_id, submission),
        stack.workflow.submit_first_review(story.story_id, submission),
        return_exceptions=True,
    )
    assert all(not isinstance(result, Exception) for result in results)
    assert stack.generator.script_calls == 1
    assert stack.synthesizer.calls == 0

    ready = await stack.workflow.get(story.story_id)
    pending = await _approve_script_review(stack, ready.story_id, ready.version)
    request = SynthesizeStoryRequest(expected_story_version=pending.version)
    results = await asyncio.gather(
        stack.workflow.synthesize(pending.story_id, request),
        stack.workflow.synthesize(pending.story_id, request),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Exception) for result in results) == 1
    assert stack.synthesizer.calls == 1

    changed_request = submission.model_copy(update={"note": "different request"})
    with pytest.raises(IdempotencyConflictError):
        await stack.workflow.submit_first_review(story.story_id, changed_request)


@pytest.mark.asyncio
async def test_mismatched_audio_cannot_enter_second_review(stack: Stack) -> None:
    original_synthesize = stack.synthesizer.synthesize

    async def mismatched(story_id, script):  # type: ignore[no-untyped-def]
        audio = await original_synthesize(story_id, script)
        return audio.model_copy(update={"revision": script.revision + 1})

    stack.synthesizer.synthesize = mismatched  # type: ignore[method-assign]
    story = await stack.workflow.ingest(ingest_request())
    story = await _approve_first_review(stack, story.story_id, story.version)
    story = await _approve_script_review(stack, story.story_id, story.version)
    with pytest.raises(TTSGenerationError, match="does not match"):
        await stack.workflow.synthesize(
            story.story_id,
            SynthesizeStoryRequest(expected_story_version=story.version),
        )
    failed = await stack.workflow.get(story.story_id)
    assert failed.status is StoryStatus.PENDING_TTS
    assert failed.last_failure is not None


@pytest.mark.asyncio
async def test_final_review_revision_returns_to_script_review_before_another_tts_run(
    stack: Stack,
) -> None:
    story = await stack.workflow.ingest(ingest_request())
    story = await _approve_first_review(stack, story.story_id, story.version)
    story = await _approve_script_review(stack, story.story_id, story.version)
    story = await stack.workflow.synthesize(
        story.story_id,
        SynthesizeStoryRequest(expected_story_version=story.version),
    )
    assert story.script is not None
    revised = story.script.model_copy(
        update={
            "segments": [
                story.script.segments[0].model_copy(update={"text": "Human-revised narration."})
            ]
        }
    )

    returned = await stack.workflow.submit_second_review(
        story.story_id,
        SecondReviewSubmission(
            expected_story_version=story.version,
            decision=ReviewDecision.REQUEST_CHANGES,
            reviewer_id="final-editor",
            note="Use the revised wording.",
            revised_script=revised,
        ),
    )
    assert returned.status is StoryStatus.SCRIPT_READY
    assert returned.audio is None
    assert returned.script is not None
    assert returned.script.revision == story.script.revision + 1
    assert stack.synthesizer.calls == 1

    pending = await _approve_script_review(stack, returned.story_id, returned.version)
    regenerated = await stack.workflow.synthesize(
        pending.story_id,
        SynthesizeStoryRequest(expected_story_version=pending.version),
    )
    assert regenerated.status is StoryStatus.PENDING_SECOND_REVIEW
    assert regenerated.audio is not None
    assert stack.synthesizer.calls == 2
