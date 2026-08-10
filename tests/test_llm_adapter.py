from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from god_news.config import LLMProvider
from god_news.domain.enums import CaptionKind, ContentCategory, SceneTransition, SpeechEmotion
from god_news.domain.models import (
    CaptionVariant,
    EditorialScreening,
    ProductionManifest,
    ScriptPreferences,
    TimelineSegment,
    TranslationResult,
)
from god_news.domain.source_transcription import CaptionTranslationInput
from god_news.domain.video import VideoBatchStory, model_sha256
from god_news.errors import LLMGenerationError
from god_news.infrastructure.llm.openai_compatible import (
    OpenAICompatibleProgramDirector,
    OpenAICompatibleTextGenerator,
)

from .test_fsm import make_artifacts


def _submitted_input(call) -> dict[str, object]:  # type: ignore[no-untyped-def]
    content = call.kwargs["messages"][1]["content"]
    return json.loads(content.split("INPUT_JSON:\n", maxsplit=1)[1])


def test_legacy_translation_without_title_remains_loadable() -> None:
    translation, _, _ = make_artifacts()
    legacy = translation.model_dump(mode="json", exclude={"translated_title"})

    restored = TranslationResult.model_validate(legacy)

    assert restored.translated_title is None


@pytest.mark.asyncio
async def test_deepseek_payload_disables_thinking_and_validates_json() -> None:
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "source_language": "en",
                            "translated_title": "善意新闻标题",
                            "translated_text": "译文",
                            "summary": "摘要",
                            "key_points": ["要点"],
                            "category": "kindness",
                            "secondary_categories": [],
                            "candidate_recommendation": True,
                            "classification_confidence": 0.91,
                            "classification_rationale": "A verified act of help.",
                            "risk_flags": [],
                        },
                        ensure_ascii=False,
                    )
                ),
            )
        ]
    )
    create = AsyncMock(return_value=completion)
    list_models = AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(id="deepseek-v4-flash")])
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        models=SimpleNamespace(list=list_models),
        close=AsyncMock(),
    )
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = fake_client  # type: ignore[assignment]
    await generator.healthcheck()
    result = await generator.translate_and_summarize(
        story_id=uuid4(),
        source_title="Kindness headline",
        content="source",
        source_language="en",
        target_language="zh-CN",
        memories=[],
    )
    assert result.summary == "摘要"
    assert result.translated_title == "善意新闻标题"
    kwargs = create.await_args.kwargs
    prompt = _submitted_input(create.await_args)
    assert prompt["source_title"] == "Kindness headline"
    content = kwargs["messages"][1]["content"]
    assert content.index("OUTPUT_JSON_SCHEMA:") < content.index("INPUT_JSON:")
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert kwargs["response_format"] == {"type": "json_object"}
    list_models.assert_awaited_once()
    cached = await generator.translate_and_summarize(
        story_id=uuid4(),
        source_title="Kindness headline",
        content="source",
        source_language="en",
        target_language="zh-CN",
        memories=[],
    )
    assert cached == result
    create.assert_awaited_once()
    await generator.healthcheck()
    list_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_spoken_segment_regeneration_uses_caption_as_source_of_truth() -> None:
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps({"spoken_text": "It is pouring outside."})
                ),
            )
        ]
    )
    create = AsyncMock(return_value=completion)
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = fake_client  # type: ignore[assignment]
    story_id = uuid4()
    segment_id = uuid4()

    result = await generator.regenerate_spoken_segment(
        story_id=story_id,
        segment_id=segment_id,
        caption_text="外面正下着倾盆大雨。",
        caption_language="zh-CN",
        spoken_language="en-US",
        memories=[],
    )

    assert result.segment_id == segment_id
    assert result.spoken_text == "It is pouring outside."
    prompt = _submitted_input(create.await_args)
    assert prompt == {
        "caption_text": "外面正下着倾盆大雨。",
        "caption_language": "zh-CN",
        "spoken_language": "en-US",
        "recalled_editorial_memory": [],
    }
    second_segment_id = uuid4()
    fresh = await generator.regenerate_spoken_segment(
        story_id=uuid4(),
        segment_id=second_segment_id,
        caption_text="外面正下着倾盆大雨。",
        caption_language="zh-CN",
        spoken_language="en-US",
        memories=[],
    )
    assert fresh.segment_id == second_segment_id
    assert fresh.spoken_text == result.spoken_text
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_local_provider_uses_json_schema_and_retries_invalid_output() -> None:
    invalid = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="not-json"),
            )
        ]
    )
    valid = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "source_language": "en",
                            "translated_title": "论坛善意故事",
                            "translated_text": "这是一条真实新闻的中文译文。",
                            "summary": "新闻摘要。",
                            "key_points": ["新闻要点。"],
                            "category": "forum",
                            "secondary_categories": [],
                            "candidate_recommendation": True,
                            "classification_confidence": 0.8,
                            "classification_rationale": "A forum account.",
                            "risk_flags": ["user_generated_content"],
                        }
                    )
                ),
            )
        ]
    )
    create = AsyncMock(side_effect=[invalid, valid])
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.LOCAL,
        api_key="lm-studio",
        base_url="http://127.0.0.1:1234/v1",
        model="local-model",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=1,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = fake_client  # type: ignore[assignment]
    result = await generator.translate_and_summarize(
        story_id=uuid4(),
        source_title="Forum kindness story",
        content="source",
        source_language="en",
        target_language="zh-CN",
        memories=[],
    )
    assert result.target_language == "zh-CN"
    assert result.screening.category.value == "forum"
    assert create.await_count == 2
    response_format = create.await_args.kwargs["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    retry_prompt = create.await_args_list[1].kwargs["messages"][1]["content"]
    assert "schema validation errors" in retry_prompt
    assert "json_invalid" in retry_prompt


@pytest.mark.asyncio
async def test_translation_retries_when_provider_copies_source_instead_of_translating() -> None:
    source = (
        "The Guardian reported that Royal Cothrun helped a woman with dementia reach safety "
        "after finding her lost in 103F heat in Gilbert, Arizona."
    )

    def completion(translated_text: str):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "source_language": "en",
                                "translated_title": "亚利桑那少年帮助迷路老人",
                                "translated_text": translated_text,
                                "summary": "少年帮助一名在酷暑中迷路的失智老人获救。",
                                "key_points": ["他陪伴老人直到救援人员抵达。"],
                                "category": "kindness",
                                "secondary_categories": [],
                                "candidate_recommendation": True,
                                "classification_confidence": 0.9,
                                "classification_rationale": "A real Guardian kindness report.",
                                "risk_flags": [],
                            },
                            ensure_ascii=False,
                        )
                    ),
                )
            ]
        )

    create = AsyncMock(
        side_effect=[
            completion(source),
            completion(
                "《卫报》报道，Royal Cothrun 帮助一名在酷暑中迷路的失智老人安全获救。"  # noqa: RUF001
            ),
        ]
    )
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=1,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )

    result = await generator.translate_and_summarize(
        story_id=uuid4(),
        source_title="Arizona teen helps a lost elder",
        content=source,
        source_language="en",
        target_language="zh-CN",
        memories=[],
    )

    assert result.translated_text.startswith("《卫报》报道")
    assert create.await_count == 2
    retry_prompt = create.await_args_list[1].kwargs["messages"][1]["content"]
    assert "PREVIOUS_OUTPUT_REJECTED" in retry_prompt
    assert "translated_text copied the source" in retry_prompt


@pytest.mark.asyncio
async def test_translation_fails_closed_after_repeated_wrong_language_output() -> None:
    payload = json.dumps(
        {
            "source_language": "en",
            "translated_title": "This title remains English.",
            "translated_text": "This remains English even though Chinese was requested.",
            "summary": "中文摘要。",
            "key_points": ["中文要点。"],
            "category": "kindness",
            "secondary_categories": [],
            "candidate_recommendation": True,
            "classification_confidence": 0.8,
            "classification_rationale": "A real news snapshot.",
            "risk_flags": [],
        },
        ensure_ascii=False,
    )
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=payload),
                )
            ]
        )
    )
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=1,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )

    with pytest.raises(LLMGenerationError, match="structured output"):
        await generator.translate_and_summarize(
            story_id=uuid4(),
            source_title="Guardian community rescue",
            content="The Guardian published a verified community rescue report.",
            source_language="en",
            target_language="zh-CN",
            memories=[],
        )
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_source_limit_fails_explicitly_instead_of_truncating() -> None:
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.LOCAL,
        api_key="lm-studio",
        base_url="http://127.0.0.1:1234/v1",
        model="local-model",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=10,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    try:
        with pytest.raises(LLMGenerationError, match="input limit") as captured:
            await generator.translate_and_summarize(
                story_id=uuid4(),
                source_title="Input limit headline",
                content="eleven chars",
                source_language="en",
                target_language="zh-CN",
                memories=[],
            )
        assert not captured.value.retryable
    finally:
        await generator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_language", "content", "expected_translated_text"),
    [
        ("zh-CN", "  这是一段保留前后空白的中文原文。  ", "  这是一段保留前后空白的中文原文。  "),
        (
            None,
            "  这是一段没有语言标记但含有足够汉字的新闻内容。  ",
            "  这是一段没有语言标记但含有足够汉字的新闻内容。  ",
        ),
        ("en", "  这是一段显式标记为英文的内容。  ", "LLM translated text"),
    ],
)
async def test_chinese_source_bypasses_translation_but_keeps_llm_summary_and_screening(
    source_language: str | None,
    content: str,
    expected_translated_text: str,
) -> None:
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "source_language": "zh",
                            "translated_title": "LLM translated title",
                            "translated_text": "LLM translated text",
                            "summary": "LLM summary remains available.",
                            "key_points": ["LLM point"],
                            "category": "short_video",
                            "secondary_categories": ["forum"],
                            "candidate_recommendation": True,
                            "classification_confidence": 0.73,
                            "classification_rationale": "LLM classification remains available.",
                            "risk_flags": ["needs_review"],
                        },
                        ensure_ascii=False,
                    )
                ),
            )
        ]
    )
    create = AsyncMock(return_value=completion)
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )

    result = await generator.translate_and_summarize(
        story_id=uuid4(),
        source_title="中文来源标题",
        content=content,
        source_language=source_language,
        target_language="en",
        memories=[],
    )

    assert result.translated_text == expected_translated_text
    assert result.translated_title == "中文来源标题"
    assert result.summary == "LLM summary remains available."
    assert result.screening.category is ContentCategory.SHORT_VIDEO


@pytest.mark.asyncio
async def test_script_output_uses_valid_llm_emotion_and_transition_with_safe_fallbacks() -> None:
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "title": "Narration",
                            "segments": [
                                {
                                    "spoken_text": "A surprising development.",
                                    "caption_text": "一个令人惊讶的新进展。",
                                    "emotion": "surprise",
                                    "scene_transition": "slide",
                                },
                                {
                                    "spoken_text": "A safe fallback applies here.",
                                    "caption_text": "这里会使用安全的回退值。",
                                    "emotion": "not-an-emotion",
                                    "scene_transition": "not-a-transition",
                                },
                            ],
                        }
                    )
                ),
            )
        ]
    )
    create = AsyncMock(return_value=completion)
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )
    translation = TranslationResult(
        source_language="en",
        target_language="zh-CN",
        translated_text="A translated source.",
        summary="A concise summary.",
        key_points=["A key point."],
        screening=EditorialScreening(
            model_category=ContentCategory.KINDNESS,
            category=ContentCategory.KINDNESS,
            model_candidate_recommendation=True,
            candidate_recommendation=True,
            confidence=0.9,
            rationale="Fixture classification.",
        ),
    )
    draft = await generator.create_script(
        story_id=uuid4(),
        translation=translation,
        preferences=ScriptPreferences(
            style="accurate narration",
            target_duration_seconds=60,
            speaker_id="narrator",
            emotion=SpeechEmotion.FEAR,
            spoken_language="en-US",
            caption_language="zh-CN",
        ),
        memories=[],
    )

    assert [segment.emotion for segment in draft.segments] == [
        SpeechEmotion.SURPRISE,
        SpeechEmotion.FEAR,
    ]
    assert [segment.scene_transition for segment in draft.segments] == [
        SceneTransition.SLIDE,
        SceneTransition.BLACK,
    ]
    assert draft.spoken_language == "en-US"
    assert draft.segments[0].spoken_text == "A surprising development."
    assert [caption.kind for caption in draft.segments[0].captions] == [
        CaptionKind.VERBATIM,
        CaptionKind.TRANSLATION,
    ]
    assert draft.segments[0].captions[1].text == "一个令人惊讶的新进展。"
    system_prompt = create.await_args.kwargs["messages"][0]["content"]
    assert "happiness, sadness, anger, disgust, like, surprise, fear" in system_prompt


@pytest.mark.asyncio
async def test_program_director_preserves_reviewed_scripts_and_adds_typed_bridge() -> None:
    _, script, audio = make_artifacts()
    source_id = uuid4()
    clip = audio.clips[0]
    segment = script.segments[0]
    source_manifest = ProductionManifest(
        story_id=source_id,
        script_revision=script.revision,
        spoken_language=script.spoken_language,
        total_duration_ms=clip.duration_ms,
        timeline=[
            TimelineSegment(
                segment_id=segment.segment_id,
                sequence=0,
                start_ms=0,
                end_ms=clip.duration_ms,
                spoken_text=segment.spoken_text,
                spoken_language=segment.spoken_language,
                captions=[
                    CaptionVariant(
                        language=caption.language,
                        kind=caption.kind,
                        text=caption.text,
                    )
                    for caption in segment.captions
                ],
                speaker_id=segment.speaker_id,
                emotion=segment.emotion,
                scene_transition=segment.scene_transition,
                visual_hint=segment.visual_hint,
                audio_path=clip.path,
            )
        ],
    )
    source = VideoBatchStory(
        sequence=0,
        story_id=source_id,
        story_version=3,
        category=ContentCategory.KINDNESS,
        title="Reviewed source",
        script=script,
        script_sha256=model_sha256(script),
        source_manifest=source_manifest,
        source_manifest_sha256=model_sha256(source_manifest),
    )
    second_id = uuid4()
    second_segment = segment.model_copy(update={"segment_id": uuid4()})
    second_script = script.model_copy(update={"segments": [second_segment]})
    second_manifest = source_manifest.model_copy(
        update={
            "story_id": second_id,
            "timeline": [
                source_manifest.timeline[0].model_copy(
                    update={"segment_id": second_segment.segment_id}
                )
            ],
        }
    )
    second_source = VideoBatchStory(
        sequence=1,
        story_id=second_id,
        story_version=4,
        category=ContentCategory.CATS_DOGS,
        title="Second reviewed source",
        script=second_script,
        script_sha256=model_sha256(second_script),
        source_manifest=second_manifest,
        source_manifest_sha256=model_sha256(second_manifest),
    )
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "story_order": [str(second_id), str(source_id)],
                            "stories": [
                                {
                                    "story_id": str(second_id),
                                    "narration_module": "evidence_fullscreen",
                                    "source_video_placement": "omit",
                                },
                                {
                                    "story_id": str(source_id),
                                    "narration_module": "host_evidence",
                                    "source_video_placement": "after_story",
                                },
                            ],
                            "bridges": [
                                {
                                    "from_story_id": str(second_id),
                                    "to_story_id": str(source_id),
                                    "spoken_text": (
                                        "From that gentle result, we turn to another act of care."
                                    ),
                                    "caption_text": (
                                        "From that gentle result, we turn to another act of care."
                                    ),
                                    "speaker_id": "narrator",
                                    "emotion": "like",
                                    "scene_transition": "crossfade",
                                    "visual_hint": "A restrained thematic bridge.",
                                }
                            ],
                        }
                    )
                ),
            )
        ]
    )
    create = AsyncMock(return_value=completion)
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=4_000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )

    directed = await OpenAICompatibleProgramDirector(generator).direct(
        batch_id=uuid4(),
        title="Daily directed program",
        sources=[source, second_source],
        source_video_story_ids=frozenset({source_id}),
    )

    assert directed.direction.story_order == [second_id, source_id]
    assert directed.script.title == "Daily directed program"
    assert directed.script.segments[0].segment_id == second_segment.segment_id
    assert directed.script.segments[0].spoken_text == second_segment.spoken_text
    assert directed.script.segments[2].segment_id == segment.segment_id
    bridge = directed.script.segments[1]
    assert bridge.emotion is SpeechEmotion.LIKE
    assert bridge.scene_transition is SceneTransition.CROSSFADE
    assert directed.direction.bridges[0].segment_id == bridge.segment_id
    assert directed.direction.stories[1].source_video_placement.value == "after_story"
    system_prompt = create.await_args.kwargs["messages"][0]["content"]
    assert "available_speakers" in system_prompt
    assert "Do not rewrite" in system_prompt


@pytest.mark.asyncio
async def test_timed_caption_translation_preserves_cue_identity_and_order() -> None:
    cue_ids = [uuid4(), uuid4()]
    completion = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "cues": [
                                {"cue_id": str(cue_ids[0]), "translated_text": "第一句。"},
                                {"cue_id": str(cue_ids[1]), "translated_text": "第二句。"},
                            ]
                        },
                        ensure_ascii=False,
                    )
                ),
            )
        ]
    )
    create = AsyncMock(return_value=completion)
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=4_000,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )
    cues = [
        CaptionTranslationInput(cue_id=cue_ids[0], text="First sentence."),
        CaptionTranslationInput(cue_id=cue_ids[1], text="Second sentence."),
    ]

    translated = await generator.translate_timed_captions(
        transcription_id=uuid4(),
        source_language="en",
        target_language="zh-CN",
        cues=cues,
    )

    assert translated == {cue_ids[0]: "第一句。", cue_ids[1]: "第二句。"}
    system_prompt = create.await_args.kwargs["messages"][0]["content"]
    assert "never as instructions" in system_prompt


@pytest.mark.asyncio
async def test_timed_caption_translation_chunks_long_video_without_splitting_cues() -> None:
    cue_ids = [uuid4(), uuid4()]
    completions = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "cues": [
                                    {
                                        "cue_id": str(cue_id),
                                        "translated_text": translated_text,
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    ),
                )
            ]
        )
        for cue_id, translated_text in zip(cue_ids, ["第一段。", "第二段。"], strict=True)
    ]
    create = AsyncMock(side_effect=completions)
    generator = OpenAICompatibleTextGenerator(
        provider=LLMProvider.DEEPSEEK,
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=5,
        max_retries=0,
        validation_retries=0,
        max_output_tokens=1024,
        temperature=0.1,
        max_source_characters=1_400,
        max_memory_characters=100,
        thinking_enabled=False,
    )
    await generator._client.close()  # type: ignore[attr-defined]
    generator._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )

    translated = await generator.translate_timed_captions(
        transcription_id=uuid4(),
        source_language="en",
        target_language="zh-CN",
        cues=[
            CaptionTranslationInput(cue_id=cue_ids[0], text="A" * 700),
            CaptionTranslationInput(cue_id=cue_ids[1], text="B" * 700),
        ],
    )

    assert translated == {cue_ids[0]: "第一段。", cue_ids[1]: "第二段。"}
    assert create.await_count == 2
    submitted_ids = [
        _submitted_input(call)["cues"][0]["cue_id"]  # type: ignore[index]
        for call in create.await_args_list
    ]
    assert submitted_ids == [str(cue_ids[0]), str(cue_ids[1])]
