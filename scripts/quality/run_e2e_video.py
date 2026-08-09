# ruff: noqa: RUF001 -- multilingual real-news fixtures intentionally mix scripts.
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import wave
from collections.abc import AsyncIterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from god_news.application.video_batches import VideoBatchService
from god_news.config import Settings, get_settings
from god_news.container import AppContainer, build_container
from god_news.domain.enums import (
    CaptionKind,
    ContentCategory,
    ReviewDecision,
    SceneTransition,
    SourceKind,
    SpeechEmotion,
    StoryStatus,
)
from god_news.domain.models import (
    AudioBundle,
    AudioClip,
    CaptionVariant,
    EditorialScreening,
    ProductionManifest,
    ScriptDocument,
    ScriptPreferences,
    ScriptSegment,
    SecondReviewSubmission,
    SourceSnapshot,
    Story,
    SynthesisMetadata,
    TimelineSegment,
    TranslationResult,
    utc_now,
)
from god_news.domain.source_transcription import (
    TimedCaptionCue,
    TranscriptReview,
    TranscriptReviewDecision,
)
from god_news.domain.video import (
    CreateVideoBatch,
    DirectedProgramDraft,
    EpisodeHostSlot,
    EpisodeSceneModule,
    NarrationReviewDecision,
    ProgramDirectorPlan,
    SourceVideoAudioMode,
    SourceVideoPlacement,
    SourceVideoRenderAsset,
    SubmitNarrationReview,
    SubmitTimelineReview,
    SynthesizeBatchNarration,
    TimelineReviewDecision,
    VideoBatch,
    VideoBatchStory,
    VideoLayout,
    VideoOutputProfileId,
    VideoRenderArtifact,
)
from god_news.domain.video_ports import ProgramDirector
from god_news.domain.visual_assets import UploadSegmentVisualAssetRequest
from god_news.infrastructure.llm.openai_compatible import (
    OpenAICompatibleProgramDirector,
    OpenAICompatibleTextGenerator,
)
from god_news.infrastructure.source_media_probe import FFprobeSourceVideoInspector
from god_news.infrastructure.tts.dsakiko import load_dsakiko_voice_assets
from god_news.infrastructure.video_assets import LocalBgmCatalog
from god_news.infrastructure.video_live2d import LocalLive2DHostRenderer
from god_news.infrastructure.video_remotion import LocalRemotionBatchVideoRenderer
from god_news.infrastructure.video_repository import SqlAlchemyVideoBatchRepository
from god_news.infrastructure.video_visual_assets import ApprovedVisualAssetLibrary
from god_news.infrastructure.visual_asset_store import LocalVisualAssetStore
from god_news.infrastructure.visual_repository import SqlAlchemyVisualAssetRepository
from god_news.operations.models import (
    RoleKind,
    RoleProfile,
    RoleProfileCreate,
    RoleVisualAssets,
)
from god_news.sources.models import ActiveSourceName
from god_news.video_errors import VideoRenderingError

WORKSPACE = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class RealNewsStorySpec:
    source: ActiveSourceName
    title: str
    source_url: str
    source_language: str
    original_text: str
    spoken_text: str
    caption_text: str
    source_captions: tuple[tuple[str, str], ...]
    category: ContentCategory
    tone_hz: int


APPROVED_NEWS_SOURCE_HOSTS: dict[ActiveSourceName, frozenset[str]] = {
    "dazhong": frozenset({"m.dzplus.dzng.com"}),
    "reddit": frozenset({"reddit.com", "www.reddit.com"}),
    "guardian": frozenset({"www.theguardian.com"}),
    "pikabu": frozenset({"pikabu.ru"}),
}


REAL_NEWS_STORIES: tuple[RealNewsStorySpec, ...] = (
    RealNewsStorySpec(
        source="guardian",
        title="Arizona teen hailed for helping save woman with dementia wandering in 103F heat",
        source_url="https://www.theguardian.com/us-news/2026/jul/18/arizona-teen-rescue-woman-dementia-heatwave",
        source_language="en-GB",
        original_text=(
            "Fourteen-year-old Royal Cothrun found Theresa Morgan, who has dementia, lost in "
            "103F heat in Gilbert, Arizona. He moved her into shade, contacted her family, and "
            "stayed until emergency responders arrived."
        ),
        spoken_text="十四歳の少年が、猛暑の中で迷っていた高齢女性を見つけ、救助が来るまで寄り添いました。",
        caption_text="一名十四岁少年帮助在酷暑中迷路的失智老人安全获救。",
        source_captions=(
            (
                "Royal Cothrun noticed that Theresa Morgan needed help.",
                "Royal Cothrun 注意到 Theresa Morgan 需要帮助。",
            ),
            ("The temperature had reached 103F.", "当时气温达到华氏一百零三度。"),
            ("He stayed until emergency responders arrived.", "他一直陪伴老人，直到救援人员抵达。"),
        ),
        category=ContentCategory.KINDNESS,
        tone_hz=330,
    ),
    RealNewsStorySpec(
        source="guardian",
        title="The kindness of strangers: another diner paid my Father's Day bill",
        source_url="https://www.theguardian.com/lifeandstyle/2026/jul/20/kindness-of-strangers-grief-loss-fathers-day-diner-who-paid-my-bill",
        source_language="en-GB",
        original_text=(
            "After losing his wife and son, Allen Renfrey ate alone at a Vietnamese restaurant on "
            "Father's Day. An unidentified couple quietly paid his bill because they did not want "
            "him to spend the occasion alone."
        ),
        spoken_text="父の日に一人で食事をしていた男性へ、見知らぬ夫婦がそっと代金を支払いました。",
        caption_text="一对陌生夫妇悄悄为父亲节独自用餐的男子付了账。",
        source_captions=(
            ("Allen Renfrey was dining alone on Father's Day.", "Allen Renfrey 在父亲节独自用餐。"),
            ("Another couple paid his bill before leaving.", "另一对夫妇离开前替他付了账。"),
            ("The anonymous kindness moved him deeply.", "这份匿名善意令他深受感动。"),
        ),
        category=ContentCategory.KINDNESS,
        tone_hz=392,
    ),
    RealNewsStorySpec(
        source="guardian",
        title="The kindness of strangers: our hotel's owners were heaven-sent",
        source_url="https://www.theguardian.com/lifeandstyle/2026/jul/27/the-kindness-of-strangers-when-mum-was-hospitalised-on-a-family-holiday-our-hotels-owners-were-heaven-sent",
        source_language="en-GB",
        original_text=(
            "When Melanie Brock's mother suffered a brain haemorrhage during a family holiday in "
            "France, hotel owners Jean-Claude and Therese cooked for the children, washed their "
            "clothes, found English-speaking support, and helped arrange accommodation."
        ),
        spoken_text="旅先で母親が倒れた家族を、ホテルの夫婦が食事や洗濯、宿泊先の手配まで支えました。",
        caption_text="母亲在异国住院后，旅馆主人从饮食、洗衣到住宿全力帮助一家人。",
        source_captions=(
            (
                "The family was far from home during a medical emergency.",
                "这家人在异国遭遇了医疗紧急情况。",
            ),
            (
                "The hotel owners cooked meals and washed clothes.",
                "旅馆主人帮孩子们做饭并清洗衣物。",
            ),
            ("They also helped the family find accommodation.", "他们还帮助一家人解决住宿问题。"),
        ),
        category=ContentCategory.KINDNESS,
        tone_hz=440,
    ),
    RealNewsStorySpec(
        source="pikabu",
        title="Сотрудница полиции вместе с друзьями спасла двух лебедят-сирот",
        source_url="https://pikabu.ru/story/sotrudnitsa_politsii_vmeste_s_druzyami_spasla_dvukh_lebedyatsirot_14207552",
        source_language="ru",
        original_text=(
            "В Мурманской области сотрудница транспортной полиции вместе с друзьями несколько "
            "дней искала двух осиротевших лебедят. Птиц нашли, отогрели и подготовили к передаче "
            "в Лапландский заповедник."
        ),
        spoken_text="警察官と友人たちは数日間森を捜し、親を失った二羽の白鳥を無事に保護しました。",
        caption_text="女警官和朋友们搜寻数日，终于救下两只失去父母的小天鹅。",
        source_captions=(
            ("Лебедята остались без родителей.", "两只小天鹅失去了父母。"),
            ("Поиски в лесу продолжались несколько дней.", "救援人员在森林中搜寻了数日。"),
            ("Птиц передадут в Лапландский заповедник.", "小天鹅将被送往拉普兰自然保护区。"),
        ),
        category=ContentCategory.KINDNESS,
        tone_hz=494,
    ),
    RealNewsStorySpec(
        source="dazhong",
        title="开屏见“好”｜青岛“90后”小伙医院门口送免费餐，引发全城爱心接力",
        source_url="https://m.dzplus.dzng.com/share/general/0/NEWS3581000EHEVSBXUWQQSM",
        source_language="zh-CN",
        original_text=(
            "青岛平度市人民医院门口，任秉凯每天傍晚为患者家属免费提供热饭菜。"
            "邻里陆续送来蔬菜、大米、牛奶等物资，一周内爱心餐从三十多份增加到近六十份。"
        ),
        spoken_text="病院の前で始まった無料の夕食が、町の人々をつなぐ支援の輪へ広がりました。",
        caption_text="医院门口的一份免费晚餐，逐渐汇成全城参与的爱心接力。",
        source_captions=(
            ("任秉凯每天傍晚为患者家属免费送饭。", "任秉凯每天傍晚为患者家属免费送饭。"),
            ("许多市民主动捐赠食材和物资。", "许多市民主动捐赠食材和物资。"),
            ("爱心餐一周内增加到每天近六十份。", "爱心餐一周内增加到每天近六十份。"),
        ),
        category=ContentCategory.KINDNESS,
        tone_hz=523,
    ),
)


class FixtureStoryPool:
    def __init__(
        self,
        stories: Sequence[Story],
        manifests: dict[UUID, ProductionManifest],
    ) -> None:
        self._stories = {story.story_id: story for story in stories}
        self._manifests = manifests

    async def get(self, story_id: UUID) -> Story:
        return self._stories[story_id]

    async def list(
        self,
        *,
        status: StoryStatus | None,
        limit: int,
        offset: int,
    ) -> Sequence[Story]:
        values = list(self._stories.values())
        if status is not None:
            values = [story for story in values if story.status is status]
        return values[offset : offset + limit]

    async def production_manifest(self, story_id: UUID) -> ProductionManifest:
        return self._manifests[story_id]


class FixtureSourceVideoLibrary:
    def __init__(self, assets: Sequence[SourceVideoRenderAsset]) -> None:
        self._assets = list(assets)

    async def approved_for_stories(
        self,
        story_ids: Sequence[UUID],
    ) -> Sequence[SourceVideoRenderAsset]:
        selected = set(story_ids)
        return [asset for asset in self._assets if asset.story_id in selected]


class RequiredDemoPolicyDirector:
    """Keep LLM ordering and bridges while enforcing the demo's acceptance policy."""

    def __init__(self, base: ProgramDirector) -> None:
        self._base = base

    @property
    def name(self) -> str:
        return f"{self._base.name}+e2e-policy"

    async def direct(
        self,
        *,
        batch_id: UUID,
        title: str,
        sources: Sequence[VideoBatchStory],
        source_video_story_ids: frozenset[UUID],
    ) -> DirectedProgramDraft:
        draft = await self._base.direct(
            batch_id=batch_id,
            title=title,
            sources=sources,
            source_video_story_ids=source_video_story_ids,
        )
        directed_stories = [
            story.model_copy(
                update={
                    "narration_module": (
                        EpisodeSceneModule.HOST_EVIDENCE
                        if index == 0
                        else (
                            EpisodeSceneModule.EVIDENCE_FULLSCREEN
                            if index == 1
                            else story.narration_module
                        )
                    ),
                    "source_video_placement": (
                        SourceVideoPlacement.AFTER_STORY
                        if story.story_id in source_video_story_ids
                        else SourceVideoPlacement.OMIT
                    ),
                }
            )
            for index, story in enumerate(draft.direction.stories)
        ]
        direction = ProgramDirectorPlan(
            story_order=draft.direction.story_order,
            stories=directed_stories,
            bridges=draft.direction.bridges,
        )
        return DirectedProgramDraft(direction=direction, script=draft.script)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and verify one complete dual-profile god-news demonstration "
            "through the real program, TTS, Live2D, and Remotion boundaries."
        )
    )
    parser.add_argument(
        "--dsakiko-root",
        type=Path,
        default=os.environ.get("GOD_NEWS_E2E_DSAKIKO_ROOT"),
        help="DSakiko installation root. May also use GOD_NEWS_E2E_DSAKIKO_ROOT.",
    )
    parser.add_argument("--character", default="soyo")
    parser.add_argument("--speaker-id", default="dsakiko-soyo")
    parser.add_argument("--source-duration-seconds", type=int, default=18)
    parser.add_argument("--live2d-size", type=int, default=512)
    parser.add_argument("--live2d-fps", type=int, default=30)
    parser.add_argument("--render-timeout-seconds", type=float, default=7_200)
    parser.add_argument("--render-concurrency", type=int, default=8)
    parser.add_argument("--render-attempts", type=int, default=2)
    parser.add_argument(
        "--title",
        default="全球好消息系统演示 · 自制合法素材",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def discover_compositor_binary(workspace: Path, name: str) -> Path:
    executable = f"{name}.exe" if os.name == "nt" else name
    matches = sorted(
        path.resolve()
        for path in (workspace / "node_modules" / ".pnpm").glob(
            f"@remotion+compositor-*/node_modules/@remotion/compositor-*/{executable}"
        )
        if path.is_file()
    )
    if not matches:
        raise RuntimeError(f"Remotion compositor binary is unavailable: {executable}")
    return matches[-1]


async def run_process(
    *args: str,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[bytes, bytes]:
    creationflags = 0x08000000 | 0x00000200 if os.name == "nt" else 0
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        detail = stderr[-4_000:].decode("utf-8", errors="replace")
        raise RuntimeError(f"Subprocess failed ({process.returncode}): {detail}")
    return stdout, stderr


def write_evidence_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * 16_000)


async def generate_source_video(
    *,
    ffmpeg: Path,
    path: Path,
    duration_seconds: int,
    image_path: Path,
    screenshot_path: Path,
    tone_hz: int,
) -> None:
    """Create one finite, project-owned documentary clip from reviewed rasters.

    The two shots each have their own deterministic camera move and are joined
    once. No short video is looped to manufacture duration.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    first_duration = duration_seconds / 2
    second_duration = duration_seconds - first_duration
    first_frames = max(1, round(first_duration * 30))
    second_frames = max(1, round(second_duration * 30))
    transition_duration = min(1.0, first_duration / 4, second_duration / 4)
    transition_offset = first_duration - transition_duration
    filter_graph = (
        f"[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
        f"crop=1280:720,scale=w='trunc((1280+120*n/{first_frames})/2)*2':"
        f"h='trunc((720+68*n/{first_frames})/2)*2':eval=frame,"
        "crop=1280:720,fps=30,setpts=PTS-STARTPTS,setsar=1/1[v0];"
        f"[1:v]scale=1280:720:force_original_aspect_ratio=increase,"
        f"crop=1280:720,scale=w='trunc((1400-120*n/{second_frames})/2)*2':"
        f"h='trunc((788-68*n/{second_frames})/2)*2':eval=frame,"
        "crop=1280:720,fps=30,setpts=PTS-STARTPTS,setsar=1/1[v1];"
        f"[v0][v1]xfade=transition=fade:duration={transition_duration:.3f}:"
        f"offset={transition_offset:.3f}[video]"
    )
    await run_process(
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-t",
        f"{first_duration:.3f}",
        "-i",
        str(image_path),
        "-loop",
        "1",
        "-t",
        f"{second_duration:.3f}",
        "-i",
        str(screenshot_path),
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={tone_hz}:sample_rate=48000:duration={duration_seconds}",
        "-filter_complex",
        filter_graph,
        "-map",
        "[video]",
        "-map",
        "2:a",
        "-t",
        str(duration_seconds),
        "-af",
        "volume=0.035",
        "-c:v",
        "h264_mf",
        "-b:v",
        "5M",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(path),
        cwd=path.parent,
        timeout_seconds=max(300, duration_seconds * 15),
    )


def ensure_role_request(
    *,
    dsakiko_root: Path,
    character: str,
    speaker_id: str,
) -> RoleProfileCreate:
    character_root = dsakiko_root / "reference_audio" / character
    config_path = character_root / "reference_audio_and_text.json"
    voice_assets = load_dsakiko_voice_assets(
        config_path=config_path,
        dsakiko_root=dsakiko_root,
    )
    model_root = character_root / "GPT-SoVITS_models"
    gpt_weights = sorted(model_root.glob("*.ckpt"))
    sovits_weights = sorted(model_root.glob("*.pth"))
    if len(gpt_weights) != 1 or len(sovits_weights) != 1:
        raise RuntimeError(
            "E2E role import requires exactly one GPT .ckpt and one SoVITS .pth."
        )
    live2d_model = (
        dsakiko_root / "live2d_related" / character / "live2D_model" / "3.model.json"
    ).resolve(strict=True)
    return RoleProfileCreate(
        slug=f"dsakiko-{character}",
        display_name=f"DSakiko {character.title()}",
        kind=RoleKind.HOST,
        speaker_id=speaker_id,
        character_prompt="Warm, restrained Japanese global-good-news host.",
        default_emotion=SpeechEmotion.LIKE.value,
        default_spoken_language="ja",
        default_speed=1.0,
        default_pitch=0.0,
        gpt_weights_path=str(gpt_weights[0].resolve(strict=True)),
        sovits_weights_path=str(sovits_weights[0].resolve(strict=True)),
        tts_model_profile="v2Pro",
        reference_language=voice_assets.reference_language,
        emotion_refs=voice_assets.emotion_refs,
        tts_enabled=True,
        visual_assets=RoleVisualAssets(live2d_asset_ref=str(live2d_model)),
        enabled=True,
    )


async def ensure_role(
    container: AppContainer,
    request: RoleProfileCreate,
) -> RoleProfile:
    service = container.role_profiles
    if service is None:
        raise RuntimeError("Role profile service is unavailable.")
    existing = await service.list()
    matches = [role for role in existing if role.speaker_id == request.speaker_id]
    if not matches:
        return await service.create(request)
    if len(matches) != 1:
        raise RuntimeError("Multiple roles own the E2E speaker_id.")
    role = matches[0]
    for field_name in RoleProfileCreate.model_fields:
        if getattr(role, field_name) != getattr(request, field_name):
            raise RuntimeError(
                "The existing E2E role differs from the requested DSakiko import. "
                "Resolve it through the versioned role UI before rerunning."
            )
    return role


def build_story(
    *,
    spec: RealNewsStorySpec,
    speaker_id: str,
    source_audio: Path,
    index: int,
) -> tuple[Story, ProductionManifest]:
    original_text = spec.original_text
    source_url = spec.source_url
    source_host = (urlparse(source_url).hostname or "").lower()
    if source_host not in APPROVED_NEWS_SOURCE_HOSTS[spec.source]:
        raise ValueError(
            f"E2E source URL host {source_host!r} does not match approved source {spec.source!r}."
        )
    source = SourceSnapshot(
        kind=SourceKind.URL,
        source_uri=source_url,
        final_uri=source_url,
        title=spec.title,
        detected_language=spec.source_language,
        fetcher=f"source-contract:{spec.source}",
        content_sha256=hashlib.sha256(original_text.encode("utf-8")).hexdigest(),
    )
    segment = ScriptSegment(
        sequence=0,
        spoken_text=spec.spoken_text,
        spoken_language="ja",
        captions=[
            CaptionVariant(
                language="ja",
                kind=CaptionKind.VERBATIM,
                text=spec.spoken_text,
            ),
            CaptionVariant(
                language="zh-CN",
                kind=CaptionKind.TRANSLATION,
                text=spec.caption_text,
            ),
        ],
        speaker_id=speaker_id,
        emotion=(SpeechEmotion.LIKE if index % 2 == 0 else SpeechEmotion.HAPPINESS),
        scene_transition=(
            SceneTransition.CROSSFADE if index % 2 == 0 else SceneTransition.SLIDE
        ),
        speed=1.0,
        pitch=0.0,
        visual_hint=f"Evidence panel for: {spec.title}",
    )
    script = ScriptDocument(
        title=spec.title,
        spoken_language="ja",
        segments=[segment],
    )
    source_audio_sha = sha256_file(source_audio)
    source_clip = AudioClip(
        segment_id=segment.segment_id,
        path=str(source_audio),
        duration_ms=1_000,
        sample_rate_hz=16_000,
        channels=1,
        sha256=source_audio_sha,
    )
    digest = "0" * 64
    audio = AudioBundle(
        revision=script.revision,
        provider="real-news-snapshot-evidence",
        model_identity="fixture-only-not-rendered",
        synthesis=SynthesisMetadata(
            seed=0,
            reference_audio_sha256=digest,
            runtime_config_sha256=digest,
            gpt_weights_sha256=digest,
            sovits_weights_sha256=digest,
            prompt_language="ja",
            text_language="ja",
        ),
        clips=[source_clip],
    )
    story = Story(
        status=StoryStatus.PENDING_SECOND_REVIEW,
        title=spec.title,
        source=cast(SourceSnapshot, source),
        original_text=original_text,
        target_language="zh-CN",
        preferences=ScriptPreferences(
            style="warm, concise global-good-news bulletin",
            target_duration_seconds=8,
            speaker_id=speaker_id,
            emotion=segment.emotion,
            speed=1.0,
            pitch=0.0,
            spoken_language="ja",
            caption_language="zh-CN",
        ),
        translation=TranslationResult(
            source_language=spec.source_language,
            target_language="zh-CN",
            translated_text=spec.caption_text,
            summary=spec.caption_text,
            key_points=[spec.caption_text],
            screening=EditorialScreening(
                model_category=spec.category,
                category=spec.category,
                model_candidate_recommendation=True,
                candidate_recommendation=True,
                confidence=1.0,
                rationale=(
                    f"Real {spec.source} news snapshot retained with its canonical source URL."
                ),
            ),
        ),
        script=script,
        audio=audio,
        created_at=utc_now() + timedelta(seconds=index),
        updated_at=utc_now() + timedelta(seconds=index),
    )
    manifest = ProductionManifest(
        story_id=story.story_id,
        script_revision=script.revision,
        spoken_language=script.spoken_language,
        total_duration_ms=source_clip.duration_ms,
        timeline=[
            TimelineSegment(
                segment_id=segment.segment_id,
                sequence=0,
                start_ms=0,
                end_ms=source_clip.duration_ms,
                spoken_text=segment.spoken_text,
                spoken_language=segment.spoken_language,
                captions=segment.captions,
                speaker_id=segment.speaker_id,
                emotion=segment.emotion,
                scene_transition=segment.scene_transition,
                visual_hint=segment.visual_hint,
                audio_path=source_clip.path,
            )
        ],
    )
    return story, manifest


async def _file_body(path: Path) -> AsyncIterable[bytes]:
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            yield chunk


async def approve_story_visuals(
    *,
    container: AppContainer,
    story: Story,
    image_path: Path,
    screenshot_path: Path,
    include_editor_image: bool,
) -> Story:
    service = container.visual_assets
    if service is None:
        raise RuntimeError("Visual asset service is unavailable.")

    screenshot = await service.register_source_screenshot(
        story_id=story.story_id,
        expected_story_version=story.version,
        filename=screenshot_path.name,
        declared_content_type="image/png",
        body=_file_body(screenshot_path),
    )
    next_version = screenshot.story_version
    if include_editor_image:
        if story.script is None:
            raise RuntimeError("E2E story script is unavailable for segment image review.")
        uploaded = await service.upload_segment(
            story_id=story.story_id,
            segment_id=story.script.segments[0].segment_id,
            request=UploadSegmentVisualAssetRequest(
                expected_story_version=next_version,
                expected_script_revision=story.script.revision,
                filename=image_path.name,
            ),
            declared_content_type="image/png",
            body=_file_body(image_path),
        )
        next_version = uploaded.story_version

    return await container.workflow.submit_second_review(
        story.story_id,
        SecondReviewSubmission(
            expected_story_version=next_version,
            decision=ReviewDecision.APPROVE,
            reviewer_id="e2e-visual-reviewer",
            note=(
                "Development-only approval of project-generated visual evidence for a "
                "real-news source snapshot."
            ),
        ),
    )


def timed_source_captions(
    spec: RealNewsStorySpec,
    duration_ms: int,
) -> list[TimedCaptionCue]:
    count = len(spec.source_captions)
    cues: list[TimedCaptionCue] = []
    for index, (source_text, translated_text) in enumerate(spec.source_captions):
        start = round(duration_ms * index / count)
        end = round(duration_ms * (index + 1) / count)
        cues.append(
            TimedCaptionCue(
                sequence=index,
                start_ms=start,
                end_ms=end,
                captions=[
                    CaptionVariant(
                        language="en",
                        kind=CaptionKind.VERBATIM,
                        text=source_text,
                    ),
                    CaptionVariant(
                        language="zh-CN",
                        kind=CaptionKind.TRANSLATION,
                        text=translated_text,
                    ),
                ],
            )
        )
    return cues


async def build_source_assets(
    *,
    ffmpeg: Path,
    inspector: FFprobeSourceVideoInspector,
    source_root: Path,
    stories: Sequence[Story],
    duration_seconds: int,
    image_path: Path,
    screenshot_path: Path,
) -> list[SourceVideoRenderAsset]:
    semaphore = asyncio.Semaphore(2)

    async def build_one(
        index: int,
        story: Story,
        spec: RealNewsStorySpec,
    ) -> SourceVideoRenderAsset:
        path = source_root / f"{index + 1:02d}-{story.story_id}.mp4"
        async with semaphore:
            await generate_source_video(
                ffmpeg=ffmpeg,
                path=path,
                duration_seconds=duration_seconds,
                image_path=image_path,
                screenshot_path=screenshot_path,
                tone_hz=spec.tone_hz,
            )
        probe = await inspector.inspect(path)
        if probe.audio_codec is None:
            raise RuntimeError("Generated source fixture is missing its original audio track.")
        return SourceVideoRenderAsset(
            asset_id=uuid4(),
            story_id=story.story_id,
            transcription_id=uuid4(),
            transcription_version=2,
            transcription_review=TranscriptReview(
                reviewer_id="e2e-fixture-editor",
                decision=TranscriptReviewDecision.APPROVE,
                reviewed_version=1,
            ),
            local_path=str(path.resolve()),
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
            duration_ms=probe.duration_ms,
            width=probe.width,
            height=probe.height,
            in_ms=0,
            out_ms=probe.duration_ms,
            audio_mode=SourceVideoAudioMode.ORIGINAL,
            source_label=f"Project-owned finite documentary clip · {spec.title}",
            captions=timed_source_captions(spec, probe.duration_ms),
        )

    return list(
        await asyncio.gather(
            *[
                build_one(index, story, REAL_NEWS_STORIES[index])
                for index, story in enumerate(stories)
            ]
        )
    )


def visual_review_times(batch: VideoBatch) -> list[tuple[str, float]]:
    props = batch.remotion_props
    if props is None or props.episode_plan is None:
        raise RuntimeError("Visual review requires compiled Remotion props.")
    segments = {
        segment.segment_id: segment for segment in props.manifest.timeline
    }
    source_videos = {
        asset.asset_id: asset for asset in props.source_videos
    }
    cursor_ms = props.intro_duration_ms
    points: list[tuple[str, float]] = []
    if props.intro_duration_ms > 0:
        points.append(("intro", props.intro_duration_ms / 2_000))
    for scene in props.episode_plan.scenes:
        if scene.narration_segment_id is not None:
            segment = segments[scene.narration_segment_id]
            duration_ms = segment.end_ms - segment.start_ms
        else:
            if scene.source_video_asset_id is None:
                raise RuntimeError("Visual review found a scene without media identity.")
            asset = source_videos[scene.source_video_asset_id]
            duration_ms = asset.out_ms - asset.in_ms
        safe_margin = min(250, max(1, duration_ms // 10))
        points.extend(
            [
                (f"scene-{scene.sequence:02d}-start", (cursor_ms + safe_margin) / 1_000),
                (f"scene-{scene.sequence:02d}-middle", (cursor_ms + duration_ms / 2) / 1_000),
                (
                    f"scene-{scene.sequence:02d}-end",
                    (cursor_ms + duration_ms - safe_margin) / 1_000,
                ),
            ]
        )
        cursor_ms += duration_ms
        if props.transition_duration_ms > 0:
            points.append(
                (
                    f"transition-{scene.sequence:02d}",
                    (cursor_ms + props.transition_duration_ms / 2) / 1_000,
                )
            )
            cursor_ms += props.transition_duration_ms
    if props.outro_duration_ms > 0:
        points.append(("outro", (cursor_ms + props.outro_duration_ms / 2) / 1_000))
    # De-duplicate sub-frame-adjacent points while preserving semantic labels.
    unique: list[tuple[str, float]] = []
    for label, second in points:
        if not unique or abs(unique[-1][1] - second) >= 1 / 30:
            unique.append((label, second))
    return unique


def host_dynamic_review_windows(batch: VideoBatch) -> list[dict[str, object]]:
    props = batch.remotion_props
    if props is None or props.episode_plan is None:
        raise RuntimeError("Dynamic review requires compiled Remotion props.")
    segments = {segment.segment_id: segment for segment in props.manifest.timeline}
    source_videos = {asset.asset_id: asset for asset in props.source_videos}
    cursor_ms = props.intro_duration_ms
    candidates: list[dict[str, object]] = []
    for scene in props.episode_plan.scenes:
        if scene.narration_segment_id is not None:
            segment = segments[scene.narration_segment_id]
            duration_ms = segment.end_ms - segment.start_ms
        else:
            if scene.source_video_asset_id is None:
                raise RuntimeError("Dynamic review found a scene without media identity.")
            asset = source_videos[scene.source_video_asset_id]
            duration_ms = asset.out_ms - asset.in_ms
        if (
            scene.host_visibility.value == "visible"
            and scene.narration_segment_id is not None
            and duration_ms >= 2_200
        ):
            segment_offset_ms = min(500, max(0, duration_ms - 2_000))
            candidates.append(
                {
                    "label": f"scene-{scene.sequence:02d}",
                    "scene_sequence": scene.sequence,
                    "segment_id": str(scene.narration_segment_id),
                    "segment_offset_seconds": segment_offset_ms / 1_000,
                    "output_start_seconds": (cursor_ms + segment_offset_ms) / 1_000,
                }
            )
        cursor_ms += duration_ms + props.transition_duration_ms
    if len(candidates) < 3:
        raise RuntimeError("Final video review requires at least three visible host windows.")
    indexes = (0, len(candidates) // 2, len(candidates) - 1)
    return [candidates[index] for index in indexes]


def host_analysis_crop(
    batch: VideoBatch,
    *,
    scene_sequence: int,
    profile_id: VideoOutputProfileId,
) -> tuple[float, float, float, float]:
    props = batch.remotion_props
    if props is None or props.episode_plan is None or props.template is None:
        raise RuntimeError("Host crop compilation requires template-backed Remotion props.")
    scene = props.episode_plan.scenes[scene_sequence]
    if scene.sequence != scene_sequence or scene.host_slot is None:
        raise RuntimeError("Host crop compilation requires a visible host scene.")
    profile = next(
        item for item in props.output_profiles if item.profile_id is profile_id
    )
    layout = next(
        item
        for item in props.template.layout_preset.profiles
        if item.profile_id is profile_id
    )
    safe_x = layout.safe_area_left
    safe_y = layout.safe_area_top
    safe_width = 1 - layout.safe_area_left - layout.safe_area_right
    safe_height = 1 - layout.safe_area_top - layout.safe_area_bottom
    horizontal = profile.layout is VideoLayout.HORIZONTAL
    caption_height = 0.2 if horizontal else 0.23
    source_height = 0.055 if horizontal else 0.045
    content_height = safe_height - caption_height - source_height
    corner = scene.host_slot is EpisodeHostSlot.CORNER
    host_width = (
        layout.host_corner_width if corner else layout.host_primary_width
    )
    if corner:
        return (
            safe_x + safe_width - host_width,
            safe_y + content_height * 0.08,
            host_width,
            content_height * (0.72 if horizontal else 0.48),
        )
    if horizontal:
        return safe_x, safe_y, host_width, content_height
    return safe_x, safe_y + content_height * 0.62, safe_width, content_height * 0.38


async def extract_continuous_sequence(
    *,
    ffmpeg: Path,
    video: Path,
    target: Path,
    start_seconds: float,
    fps: int = 30,
) -> dict[str, object]:
    frames = target / "frames"
    await asyncio.to_thread(frames.mkdir, parents=True, exist_ok=True)
    await run_process(
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_seconds:.6f}",
        "-i",
        str(video),
        "-t",
        "2",
        "-vf",
        f"fps={fps}",
        str(frames / "frame-%03d.png"),
        cwd=target,
        timeout_seconds=180,
    )
    frame_paths = sorted(frames.glob("frame-*.png"))
    if len(frame_paths) < fps * 2:
        raise RuntimeError(f"Continuous visual review is too short: {target}")
    contact = target / "full-contact.png"
    await run_process(
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        str(fps),
        "-start_number",
        "1",
        "-i",
        str(frames / "frame-%03d.png"),
        "-vf",
        (
            "scale=256:144:force_original_aspect_ratio=decrease,"
            "pad=256:144:(ow-iw)/2:(oh-ih)/2:color=0x101512,"
            "tile=10x6:padding=2:margin=2"
        ),
        "-frames:v",
        "1",
        str(contact),
        cwd=target,
        timeout_seconds=180,
    )
    return {
        "start_seconds": round(start_seconds, 6),
        "duration_seconds": 2,
        "frame_count": len(frame_paths),
        "frames": [str(path.resolve()) for path in frame_paths],
        "contact_sheet": str(contact.resolve()),
    }


async def extract_visual_review_evidence(
    *,
    ffmpeg: Path,
    batch: VideoBatch,
    artifact: VideoRenderArtifact,
    frame_root: Path,
) -> dict[str, dict[str, object]]:
    await asyncio.to_thread(frame_root.mkdir, parents=True, exist_ok=True)
    review_points = visual_review_times(batch)
    dynamic_windows = host_dynamic_review_windows(batch)
    if len(review_points) < 20:
        raise RuntimeError("Visual review plan must contain at least twenty samples.")
    result: dict[str, dict[str, object]] = {}
    for output in artifact.outputs:
        paths: list[str] = []
        samples: list[dict[str, object]] = []
        for index, (label, second) in enumerate(review_points):
            path = frame_root / f"{output.profile_id.value}-frame-{index:03d}.png"
            await run_process(
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{second:.3f}",
                "-i",
                output.local_path,
                "-frames:v",
                "1",
                str(path),
                cwd=frame_root,
                timeout_seconds=120,
            )
            paths.append(str(path.resolve()))
            samples.append(
                {
                    "label": label,
                    "time_seconds": round(second, 3),
                    "path": str(path.resolve()),
                }
            )
        columns = 5
        rows = (len(paths) + columns - 1) // columns
        contact_sheet = frame_root / f"{output.profile_id.value}-contact-sheet.png"
        await run_process(
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            "1",
            "-start_number",
            "0",
            "-i",
            str(frame_root / f"{output.profile_id.value}-frame-%03d.png"),
            "-vf",
            (
                "scale=320:180:force_original_aspect_ratio=decrease,"
                "pad=320:180:(ow-iw)/2:(oh-ih)/2:color=0x101512,"
                f"tile={columns}x{rows}:padding=4:margin=4"
            ),
            "-frames:v",
            "1",
            str(contact_sheet),
            cwd=frame_root,
            timeout_seconds=180,
        )
        uniform_contact = frame_root / f"{output.profile_id.value}-uniform-20.png"
        await run_process(
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            output.local_path,
            "-vf",
            (
                f"fps=20/{output.duration_in_frames / output.fps:.6f},"
                "scale=320:180:force_original_aspect_ratio=decrease,"
                "pad=320:180:(ow-iw)/2:(oh-ih)/2:color=0x101512,"
                "tile=5x4:padding=4:margin=4"
            ),
            "-frames:v",
            "1",
            str(uniform_contact),
            cwd=frame_root,
            timeout_seconds=180,
        )
        dynamic_sequences: list[dict[str, object]] = []
        for window in dynamic_windows:
            sequence = await extract_continuous_sequence(
                ffmpeg=ffmpeg,
                video=Path(output.local_path),
                target=(
                    frame_root
                    / f"{output.profile_id.value}-{window['label']}-continuous"
                ),
                start_seconds=cast(float, window["output_start_seconds"]),
            )
            dynamic_sequences.append({**window, **sequence})
        result[output.profile_id.value] = {
            "samples": samples,
            "contact_sheet": str(contact_sheet.resolve()),
            "uniform_20_contact_sheet": str(uniform_contact.resolve()),
            "dynamic_sequences": dynamic_sequences,
        }
    return result


async def extract_layer_comparison_evidence(
    *,
    ffmpeg: Path,
    live2d_python: Path,
    batch: VideoBatch,
    artifact: VideoRenderArtifact,
    layer_root: Path,
) -> dict[str, object]:
    await asyncio.to_thread(layer_root.mkdir, parents=True, exist_ok=True)
    first_window = host_dynamic_review_windows(batch)[0]
    segment_id = UUID(cast(str, first_window["segment_id"]))
    raw_asset = next(
        asset
        for asset in batch.visual_reservations.host_videos
        if asset.segment_id == segment_id
    )
    clip = next(
        item
        for item in cast(AudioBundle, batch.narration.audio).clips
        if item.segment_id == segment_id
    )
    raw = Path(raw_asset.local_path)
    background = layer_root / "host-fixed-background.mp4"
    await run_process(
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(raw),
        "-i",
        clip.path,
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x24302a:s={raw_asset.width}x{raw_asset.height}:r={raw_asset.fps}",
        "-filter_complex",
        "[2:v][0:v]overlay=shortest=1:format=auto,format=yuv420p[out]",
        "-map",
        "[out]",
        "-map",
        "1:a",
        "-c:v",
        "h264_mf",
        "-b:v",
        "4M",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        str(background),
        cwd=layer_root,
        timeout_seconds=300,
    )
    offset = cast(float, first_window["segment_offset_seconds"])
    analysis: dict[str, object] = {}
    for label, path in (("raw_webm", raw), ("fixed_background", background)):
        analysis_path = layer_root / f"{label}-analysis.json"
        analysis_arguments = [
            str(live2d_python),
            str(WORKSPACE / "scripts" / "quality" / "analyze_live2d_video.py"),
            "--input",
            str(path),
            "--output",
            str(analysis_path),
            "--expected-fps",
            str(raw_asset.fps),
            "--start-seconds",
            f"{offset:.6f}",
            "--duration-seconds",
            "2",
            "--require-dynamic-quality",
        ]
        if label == "raw_webm":
            if raw_asset.diagnostics is None:
                raise RuntimeError("Raw Live2D layer is missing diagnostics.")
            analysis_arguments.extend(
                [
                    "--preencode-trace",
                    raw_asset.diagnostics.trace_path,
                    "--require-transparency",
                ]
            )
        else:
            analysis_arguments.extend(
                ["--quality-profile", "fixed_background"]
            )
        await run_process(
            *analysis_arguments,
            cwd=WORKSPACE,
            timeout_seconds=300,
        )
        analysis[label] = str(analysis_path)
    final_analysis: dict[str, list[dict[str, object]]] = {}
    for render_output in artifact.outputs:
        profile_reports: list[dict[str, object]] = []
        for window in host_dynamic_review_windows(batch):
            analysis_path = (
                layer_root
                / (
                    f"{render_output.profile_id.value}-{window['label']}"
                    "-analysis.json"
                )
            )
            crop = host_analysis_crop(
                batch,
                scene_sequence=cast(int, window["scene_sequence"]),
                profile_id=render_output.profile_id,
            )
            await run_process(
                str(live2d_python),
                str(WORKSPACE / "scripts" / "quality" / "analyze_live2d_video.py"),
                "--input",
                render_output.local_path,
                "--output",
                str(analysis_path),
                "--expected-fps",
                str(render_output.fps),
                "--start-seconds",
                f"{cast(float, window['output_start_seconds']):.6f}",
                "--duration-seconds",
                "2",
                "--crop-normalized",
                *(f"{value:.9f}" for value in crop),
                "--quality-profile",
                "final_composite",
                "--require-dynamic-quality",
                cwd=WORKSPACE,
                timeout_seconds=300,
            )
            profile_reports.append(
                {
                    "window": window,
                    "host_crop_normalized": list(crop),
                    "analysis": str(analysis_path.resolve()),
                }
            )
        final_analysis[render_output.profile_id.value] = profile_reports
    analysis["final_outputs"] = final_analysis
    sequences: dict[str, object] = {
        "raw_webm": await extract_continuous_sequence(
            ffmpeg=ffmpeg,
            video=raw,
            target=layer_root / "raw-webm-continuous",
            start_seconds=offset,
            fps=raw_asset.fps,
        ),
        "fixed_background": await extract_continuous_sequence(
            ffmpeg=ffmpeg,
            video=background,
            target=layer_root / "fixed-background-continuous",
            start_seconds=offset,
            fps=raw_asset.fps,
        ),
    }
    for render_output in artifact.outputs:
        sequences[render_output.profile_id.value] = await extract_continuous_sequence(
            ffmpeg=ffmpeg,
            video=Path(render_output.local_path),
            target=layer_root / f"{render_output.profile_id.value}-continuous",
            start_seconds=cast(float, first_window["output_start_seconds"]),
            fps=raw_asset.fps,
        )
    return {
        "segment_id": str(segment_id),
        "raw_webm": str(raw),
        "fixed_background_mp4": str(background),
        "final_outputs": {
            output.profile_id.value: output.local_path for output in artifact.outputs
        },
        "aligned_window": first_window,
        "analysis": analysis,
        "continuous_sequences": sequences,
    }


def build_report(
    *,
    started_at: datetime,
    finished_at: datetime,
    role: RoleProfile,
    batch: VideoBatch,
    artifact: VideoRenderArtifact,
    source_assets: Sequence[SourceVideoRenderAsset],
    frame_paths: dict[str, dict[str, object]],
    layer_comparison: dict[str, object],
    source_commit: str,
) -> dict[str, object]:
    narration = batch.narration
    remotion_props = batch.remotion_props
    if (
        narration.direction is None
        or narration.audio is None
        or remotion_props is None
        or remotion_props.episode_plan is None
    ):
        raise RuntimeError("Rendered E2E batch is missing its reviewed program evidence.")
    return {
        "schema_version": "1.0",
        "source_commit": source_commit,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "batch_id": str(batch.batch_id),
        "batch_version": batch.version,
        "program_status": batch.status.value,
        "role": {
            "profile_id": str(role.profile_id),
            "speaker_id": role.speaker_id,
            "version": role.version,
            "emotion_reference_count": len(role.emotion_refs),
            "tts_model_profile": role.tts_model_profile,
            "reference_language": role.reference_language,
            "live2d_configured": role.visual_assets.live2d_asset_ref is not None,
        },
        "director_plan": narration.direction.model_dump(mode="json"),
        "program_script": narration.script.model_dump(mode="json"),
        "program_audio": narration.audio.model_dump(mode="json"),
        "episode_plan": remotion_props.episode_plan.model_dump(mode="json"),
        "template": (
            remotion_props.template.model_dump(mode="json")
            if remotion_props.template is not None
            else None
        ),
        "visual_assets": [
            asset.model_dump(mode="json") for asset in remotion_props.visual_assets
        ],
        "source_assets": [
            {
                **asset.model_dump(mode="json", exclude={"local_path"}),
                "local_path": asset.local_path,
                "creator": "god-news project-owned demo asset pipeline",
                "rights_status": "project_owned_ai_assisted_demo",
                "attribution_required": False,
                "looped_source_video": False,
            }
            for asset in source_assets
        ],
        "host_assets": [
            asset.model_dump(mode="json")
            for asset in batch.visual_reservations.host_videos
        ],
        "render_artifact": artifact.model_dump(mode="json"),
        "visual_review_evidence": frame_paths,
        "live2d_layer_comparison": layer_comparison,
    }


async def main() -> None:
    args = parse_args()
    if args.dsakiko_root is None:
        raise SystemExit(
            "--dsakiko-root or GOD_NEWS_E2E_DSAKIKO_ROOT is required for real Live2D/TTS."
        )
    dsakiko_root = args.dsakiko_root.expanduser().resolve(strict=True)
    if args.source_duration_seconds < 10 or args.source_duration_seconds > 30:
        raise SystemExit("--source-duration-seconds must stay between 10 and 30.")
    if args.live2d_size % 2:
        raise SystemExit("--live2d-size must be even.")
    if args.render_attempts < 1 or args.render_attempts > 3:
        raise SystemExit("--render-attempts must stay between 1 and 3.")

    started_at = datetime.now(UTC)
    git_stdout, _ = await run_process(
        "git",
        "rev-parse",
        "HEAD",
        cwd=WORKSPACE,
        timeout_seconds=30,
    )
    source_commit = git_stdout.decode("ascii", errors="strict").strip()
    run_id = f"{started_at:%Y%m%dT%H%M%SZ}-{uuid4()}"
    base_settings: Settings = get_settings()
    run_root = (base_settings.output_dir / "e2e" / run_id).resolve()
    run_root.mkdir(parents=True, exist_ok=False)
    runtime_root = run_root / "runtime"
    settings = base_settings.model_copy(
        update={
            "database_url": (
                "sqlite+aiosqlite:///"
                f"{(runtime_root / 'god-news-e2e.db').resolve().as_posix()}"
            ),
            "output_dir": runtime_root / "outputs",
            "uploaded_video_dir": runtime_root / "uploads" / "videos",
            "memory_chroma_persist_directory": runtime_root / "chroma",
        }
    )
    source_root = run_root / "source-videos"
    render_root = run_root / "renders"
    live2d_root = run_root / "live2d"
    frame_root = run_root / "visual-review"
    layer_root = run_root / "live2d-layer-review"
    report_path = run_root / "artifact-report.json"
    source_audio = run_root / "source-story-evidence.wav"
    write_evidence_wav(source_audio)
    snapshot_root = WORKSPACE / "assets" / "news-snapshots" / "approved-sources-2026"
    image_path = (snapshot_root / "guardian-arizona-teen.png").resolve(strict=True)
    story_screenshots = [
        (snapshot_root / "guardian-arizona-teen.png").resolve(strict=True),
        (snapshot_root / "guardian-fathers-day.png").resolve(strict=True),
        (snapshot_root / "guardian-hotel-kindness.png").resolve(strict=True),
        (snapshot_root / "pikabu-swan-rescue.png").resolve(strict=True),
        (snapshot_root / "dazhong-free-meals.png").resolve(strict=True),
    ]

    ffmpeg = (dsakiko_root / "GPT_SoVITS" / "ffmpeg.exe").resolve(strict=True)
    ffprobe = discover_compositor_binary(WORKSPACE, "ffprobe")
    inspector = FFprobeSourceVideoInspector(ffprobe, timeout_seconds=60)

    container = await build_container(settings)
    try:
        if container.database is None or container.role_profiles is None:
            raise RuntimeError("Database-backed role and video services are required.")
        role_request = ensure_role_request(
            dsakiko_root=dsakiko_root,
            character=args.character,
            speaker_id=args.speaker_id,
        )
        role = await ensure_role(container, role_request)

        stories: list[Story] = []
        manifests: dict[UUID, ProductionManifest] = {}
        for index, spec in enumerate(REAL_NEWS_STORIES):
            story, _fixture_manifest = build_story(
                spec=spec,
                speaker_id=role.speaker_id,
                source_audio=source_audio,
                index=index,
            )
            created = await container.repository.create(story)
            approved = await approve_story_visuals(
                container=container,
                story=created,
                image_path=image_path,
                screenshot_path=story_screenshots[index],
                # The first story exercises both editor image and source-page
                # review using the same rights-documented approved-source snapshot.
                include_editor_image=index == 0,
            )
            stories.append(approved)
            manifests[approved.story_id] = await container.workflow.production_manifest(
                approved.story_id
            )

        source_assets = await build_source_assets(
            ffmpeg=ffmpeg,
            inspector=inspector,
            source_root=source_root,
            stories=stories[:1],
            duration_seconds=args.source_duration_seconds,
            image_path=image_path,
            screenshot_path=story_screenshots[0],
        )
        if not isinstance(container.generator, OpenAICompatibleTextGenerator):
            raise RuntimeError("A configured OpenAI-compatible LLM is required.")
        director = RequiredDemoPolicyDirector(
            OpenAICompatibleProgramDirector(container.generator)
        )
        host_renderer = LocalLive2DHostRenderer(
            profiles=container.role_profiles,
            python_executable=dsakiko_root / "runtime" / "python.exe",
            worker_script=(
                WORKSPACE
                / "scripts"
                / "workers"
                / "render_live2d_host.py"
            ),
            inspector=inspector,
            output_root=live2d_root,
            trusted_asset_roots=[dsakiko_root],
            timeout_seconds=900,
            max_parallel_segments=1,
            width=args.live2d_size,
            height=args.live2d_size,
            fps=args.live2d_fps,
        )
        renderer = LocalRemotionBatchVideoRenderer(
            package_dir=settings.video_remotion_package_dir,
            output_dir=render_root,
            node_command=settings.video_node_command,
            quality_ffmpeg_command=ffmpeg,
            timeout_seconds=args.render_timeout_seconds,
            max_parallel_batches=1,
            concurrency=args.render_concurrency,
        )
        visual_repository = SqlAlchemyVisualAssetRepository(
            container.database.sessions,
            storage_root=settings.visual_asset_root,
        )
        visual_store = LocalVisualAssetStore(
            settings.visual_asset_root,
            max_upload_bytes=settings.visual_asset_max_upload_bytes,
            max_pixels=settings.visual_asset_max_pixels,
        )
        service = VideoBatchService(
            story_pool=FixtureStoryPool(stories, manifests),
            repository=SqlAlchemyVideoBatchRepository(container.database.sessions),
            host_renderer=host_renderer,
            program_director=director,
            synthesizer=container.synthesizer,
            video_renderer=renderer,
            bgm_catalog=LocalBgmCatalog(run_root / "bgm"),
            source_video_library=FixtureSourceVideoLibrary(source_assets),
            visual_asset_library=ApprovedVisualAssetLibrary(
                repository=visual_repository,
                store=visual_store,
            ),
            audio_root=settings.output_dir,
        )

        batch = await service.create(
            CreateVideoBatch(
                title=args.title,
                subtitle="日语本地口播 · 中文字幕 · 已审核项目自有视觉素材",
                story_ids=[story.story_id for story in stories],
                max_stories=len(stories),
            )
        )
        batch = await service.submit_narration_review(
            batch.batch_id,
            SubmitNarrationReview(
                expected_batch_version=batch.version,
                decision=NarrationReviewDecision.APPROVE,
                reviewer_id="e2e-development-reviewer",
                note="Development-only automatic approval of sourced real-news snapshots.",
            ),
        )
        batch = await service.synthesize_narration(
            batch.batch_id,
            SynthesizeBatchNarration(expected_batch_version=batch.version),
        )
        if batch.narration_failure is not None:
            raise RuntimeError(batch.narration_failure.message)
        if batch.remotion_props is None or batch.narration.manifest is None:
            raise RuntimeError("Program synthesis completed without render semantics.")

        expected_duration_ms = (
            batch.narration.manifest.total_duration_ms
            + sum(asset.out_ms - asset.in_ms for asset in batch.remotion_props.source_videos)
            + batch.remotion_props.intro_duration_ms
            + batch.remotion_props.outro_duration_ms
            + batch.remotion_props.transition_duration_ms
            * (
                len(batch.remotion_props.episode_plan.scenes)
                if batch.remotion_props.outro_duration_ms > 0
                else len(batch.remotion_props.episode_plan.scenes) - 1
            )
        )
        if not 45_000 <= expected_duration_ms <= 180_000:
            raise RuntimeError(
                f"Compiled E2E duration {expected_duration_ms / 1000:.2f}s "
                "is outside the evidence-backed complete-program acceptance window."
            )
        batch = await service.submit_timeline_review(
            batch.batch_id,
            SubmitTimelineReview(
                expected_batch_version=batch.version,
                decision=TimelineReviewDecision.APPROVE,
                reviewer_id="e2e-development-reviewer",
                note="Development-only approval after typed timeline validation.",
                story_order=[story.story_id for story in batch.stories],
            ),
        )
        for attempt in range(1, args.render_attempts + 1):
            try:
                batch = await service.render(
                    batch.batch_id,
                    expected_version=batch.version,
                )
                break
            except VideoRenderingError as exc:
                if attempt >= args.render_attempts:
                    raise
                batch = await service.get(batch.batch_id)
                if batch.status.value != "FAILED":
                    raise RuntimeError(
                        "Render retry requires the batch to persist an explicit FAILED state."
                    ) from exc
        if not isinstance(batch.artifact, VideoRenderArtifact):
            raise RuntimeError("Real dual-profile render artifact was not produced.")

        for output in batch.artifact.outputs:
            probe = await inspector.inspect(Path(output.local_path))
            expected_profile = next(
                profile
                for profile in batch.remotion_props.output_profiles
                if profile.profile_id is output.profile_id
            )
            if (
                probe.width != expected_profile.width
                or probe.height != expected_profile.height
                or probe.audio_codec is None
                or abs(probe.duration_ms - expected_duration_ms) > 1_500
            ):
                raise RuntimeError(
                    f"Rendered profile failed media verification: {output.profile_id.value}"
                )
        frames = await extract_visual_review_evidence(
            ffmpeg=ffmpeg,
            batch=batch,
            artifact=batch.artifact,
            frame_root=frame_root,
        )
        layer_comparison = await extract_layer_comparison_evidence(
            ffmpeg=ffmpeg,
            live2d_python=(dsakiko_root / "runtime" / "python.exe").resolve(
                strict=True
            ),
            batch=batch,
            artifact=batch.artifact,
            layer_root=layer_root,
        )
        finished_at = datetime.now(UTC)
        report = build_report(
            started_at=started_at,
            finished_at=finished_at,
            role=role,
            batch=batch,
            artifact=batch.artifact,
            source_assets=source_assets,
            frame_paths=frames,
            layer_comparison=layer_comparison,
            source_commit=source_commit,
        )
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "batch_id": str(batch.batch_id),
                    "duration_ms": expected_duration_ms,
                    "outputs": [
                        {
                            "profile_id": output.profile_id.value,
                            "path": output.local_path,
                            "size_bytes": output.size_bytes,
                            "sha256": output.sha256,
                        }
                        for output in batch.artifact.outputs
                    ],
                    "report": str(report_path),
                    "frames": frames,
                    "live2d_layer_comparison": layer_comparison,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await container.aclose()


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
