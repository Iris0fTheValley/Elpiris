from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from god_news.domain.enums import (
    AudioFormat,
    CaptionKind,
    ContentCategory,
    ReviewDecision,
    ReviewStage,
    SceneTransition,
    SourceKind,
    SpeechEmotion,
    StoryStatus,
)
from god_news.sources.models import NormalizedSourceItem, RawSourceItem

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PreservedNonBlankStr = Annotated[str, StringConstraints(strip_whitespace=False, min_length=1)]


def normalize_legacy_speech_emotion(value: object) -> object:
    """Map the retired neutral default onto a concrete reference-voice emotion.

    Earlier persisted stories used ``neutral`` while GPT-SoVITS had only one
    reference voice.  The multi-emotion contract has seven concrete labels;
    treating legacy neutral narration as happiness lets existing records remain
    readable without letting new arbitrary labels enter the script contract.
    """

    if isinstance(value, str) and value.strip().casefold() == "neutral":
        return SpeechEmotion.HAPPINESS.value
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UrlSource(DomainModel):
    kind: Literal[SourceKind.URL] = SourceKind.URL
    url: AnyHttpUrl


class TextSource(DomainModel):
    kind: Literal[SourceKind.TEXT] = SourceKind.TEXT
    text: Annotated[str, StringConstraints(min_length=1, max_length=500_000)]
    title: NonBlankStr = "Uploaded text"
    language: NonBlankStr | None = None


SourceRequest = Annotated[UrlSource | TextSource, Field(discriminator="kind")]


class IngestRequest(DomainModel):
    source: SourceRequest
    target_language: NonBlankStr = "zh-CN"
    style: NonBlankStr = "clear, accurate short-video narration"
    target_duration_seconds: int = Field(default=20, ge=5, le=600)
    speaker_id: NonBlankStr = "narrator"
    emotion: SpeechEmotion = SpeechEmotion.HAPPINESS
    speed: float = Field(default=1.0, ge=0.6, le=1.65)
    pitch: float = Field(default=0.0, ge=-12.0, le=12.0)

    @field_validator("emotion", mode="before")
    @classmethod
    def normalize_legacy_emotion(cls, value: object) -> object:
        return normalize_legacy_speech_emotion(value)


class SourceItemIngestRequest(DomainModel):
    """Trusted adapter boundary for a registered source.

    Network-facing adapters must map their documented/authorized response into
    ``RawSourceItem`` before calling this boundary. The core never consumes an
    upstream provider's unvalidated mapping.
    """

    item: RawSourceItem
    target_language: NonBlankStr = "zh-CN"
    style: NonBlankStr = "clear, accurate short-video narration"
    target_duration_seconds: int = Field(default=20, ge=5, le=600)
    speaker_id: NonBlankStr = "narrator"
    emotion: SpeechEmotion = SpeechEmotion.HAPPINESS
    speed: float = Field(default=1.0, ge=0.6, le=1.65)
    pitch: float = Field(default=0.0, ge=-12.0, le=12.0)

    @field_validator("emotion", mode="before")
    @classmethod
    def normalize_legacy_emotion(cls, value: object) -> object:
        return normalize_legacy_speech_emotion(value)


class SourceSnapshot(DomainModel):
    kind: SourceKind
    source_uri: str
    final_uri: str
    title: NonBlankStr
    author: str | None = None
    published_at: datetime | None = None
    detected_language: str | None = None
    fetcher: NonBlankStr
    fetched_at: datetime = Field(default_factory=utc_now)
    content_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class FetchedDocument(DomainModel):
    source: SourceSnapshot
    content: NonBlankStr
    outbound_links: list[AnyHttpUrl] = Field(default_factory=list, max_length=500)
    video_links: list[AnyHttpUrl] = Field(default_factory=list, max_length=50)

    @classmethod
    def from_text(cls, request: TextSource) -> FetchedDocument:
        digest = sha256(request.text.encode("utf-8")).hexdigest()
        return cls(
            source=SourceSnapshot(
                kind=SourceKind.TEXT,
                source_uri="text://user-upload",
                final_uri="text://user-upload",
                title=request.title,
                detected_language=request.language,
                fetcher="text",
                content_sha256=digest,
            ),
            content=request.text,
        )

    @classmethod
    def from_normalized_source(cls, item: NormalizedSourceItem) -> FetchedDocument:
        canonical_url = str(item.canonical_url)
        return cls(
            source=SourceSnapshot(
                kind=SourceKind.FIXED_SOURCE,
                source_uri=canonical_url,
                final_uri=canonical_url,
                title=item.title,
                author=item.author,
                published_at=item.published_at,
                detected_language=item.language,
                fetcher=f"source-contract:{item.source}",
                content_sha256=item.content_sha256,
            ),
            content=item.content_text,
        )


class EditorialScreening(DomainModel):
    """AI screening evidence plus the current human-reviewable decision.

    Model fields are immutable evidence. Review edits update only ``category``
    and ``candidate_recommendation``, so acceptance metrics remain auditable.
    """

    model_category: ContentCategory
    category: ContentCategory
    model_candidate_recommendation: bool
    candidate_recommendation: bool
    confidence: float = Field(ge=0, le=1)
    rationale: NonBlankStr
    risk_flags: list[NonBlankStr] = Field(default_factory=list, max_length=20)
    secondary_categories: list[ContentCategory] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_categories(self) -> EditorialScreening:
        if self.model_category in self.secondary_categories:
            raise ValueError("primary model category cannot also be secondary")
        if len(self.secondary_categories) != len(set(self.secondary_categories)):
            raise ValueError("secondary categories must be unique")
        return self


class TranslationResult(DomainModel):
    source_language: NonBlankStr
    target_language: NonBlankStr
    # Optional for backward-compatible loading of stories created before title
    # translation became part of the structured LLM contract.
    translated_title: NonBlankStr | None = None
    # Chinese source text may deliberately bypass translation.  Preserve the
    # already-captured source bytes exactly instead of stripping editorially
    # meaningful leading/trailing whitespace a second time.
    translated_text: PreservedNonBlankStr
    summary: NonBlankStr
    key_points: list[NonBlankStr] = Field(default_factory=list, max_length=20)
    screening: EditorialScreening


class ScriptPreferences(DomainModel):
    style: NonBlankStr
    target_duration_seconds: int = Field(ge=5, le=600)
    speaker_id: NonBlankStr
    emotion: SpeechEmotion = SpeechEmotion.HAPPINESS
    speed: float = Field(default=1.0, ge=0.6, le=1.65)
    pitch: float = Field(default=0.0, ge=-12.0, le=12.0)
    spoken_language: NonBlankStr | None = None
    caption_language: NonBlankStr | None = None

    @field_validator("emotion", mode="before")
    @classmethod
    def normalize_legacy_emotion(cls, value: object) -> object:
        return normalize_legacy_speech_emotion(value)


class CaptionVariant(DomainModel):
    language: NonBlankStr
    kind: CaptionKind
    text: NonBlankStr


class ScriptSegmentDraft(DomainModel):
    spoken_text: NonBlankStr = Field(
        validation_alias=AliasChoices("spoken_text", "text")
    )
    spoken_language: NonBlankStr = "und"
    captions: list[CaptionVariant] = Field(default_factory=list, max_length=20)
    speaker_id: NonBlankStr = "narrator"
    emotion: SpeechEmotion = SpeechEmotion.HAPPINESS
    scene_transition: SceneTransition = SceneTransition.BLACK
    speed: float = Field(default=1.0, ge=0.6, le=1.65)
    pitch: float = Field(default=0.0, ge=-12.0, le=12.0)
    visual_hint: str | None = None

    @field_validator("emotion", mode="before")
    @classmethod
    def normalize_legacy_emotion(cls, value: object) -> object:
        return normalize_legacy_speech_emotion(value)

    @model_validator(mode="after")
    def validate_caption_alignment(self) -> ScriptSegmentDraft:
        self.captions = _validated_captions(
            self.captions,
            spoken_text=self.spoken_text,
            spoken_language=self.spoken_language,
        )
        return self

    @property
    def text(self) -> str:
        """Read-only compatibility for internal callers during schema migration."""

        return self.spoken_text


class ScriptDraft(DomainModel):
    title: NonBlankStr
    spoken_language: NonBlankStr = Field(
        validation_alias=AliasChoices("spoken_language", "language")
    )
    segments: list[ScriptSegmentDraft] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def apply_document_language(self) -> ScriptDraft:
        self.segments = [
            _draft_with_document_language(segment, self.spoken_language)
            for segment in self.segments
        ]
        return self

    @property
    def language(self) -> str:
        return self.spoken_language


class ScriptSegment(DomainModel):
    segment_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=0)
    spoken_text: NonBlankStr = Field(
        validation_alias=AliasChoices("spoken_text", "text")
    )
    spoken_language: NonBlankStr = "und"
    captions: list[CaptionVariant] = Field(default_factory=list, max_length=20)
    speaker_id: NonBlankStr
    emotion: SpeechEmotion
    # This describes the transition after this segment.  Renderers may use a
    # richer animation later; the current renderer can safely render black.
    scene_transition: SceneTransition = SceneTransition.BLACK
    speed: float = Field(ge=0.6, le=1.65)
    pitch: float = Field(ge=-12.0, le=12.0)
    visual_hint: str | None = None

    @field_validator("emotion", mode="before")
    @classmethod
    def normalize_legacy_emotion(cls, value: object) -> object:
        return normalize_legacy_speech_emotion(value)

    @model_validator(mode="after")
    def validate_caption_alignment(self) -> ScriptSegment:
        self.captions = _validated_captions(
            self.captions,
            spoken_text=self.spoken_text,
            spoken_language=self.spoken_language,
        )
        return self

    @property
    def text(self) -> str:
        return self.spoken_text


class ScriptDocument(DomainModel):
    revision: int = Field(default=1, ge=1)
    title: NonBlankStr
    spoken_language: NonBlankStr = Field(
        validation_alias=AliasChoices("spoken_language", "language")
    )
    segments: list[ScriptSegment] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_sequences(self) -> ScriptDocument:
        self.segments = [
            _segment_with_document_language(segment, self.spoken_language)
            for segment in self.segments
        ]
        expected = list(range(len(self.segments)))
        actual = [segment.sequence for segment in self.segments]
        if actual != expected:
            raise ValueError("script segment sequence must be contiguous and zero-based")
        ids = [segment.segment_id for segment in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("script segment_id values must be unique")
        return self

    @property
    def language(self) -> str:
        return self.spoken_language

    @classmethod
    def from_draft(cls, draft: ScriptDraft, *, revision: int = 1) -> ScriptDocument:
        return cls(
            revision=revision,
            title=draft.title,
            spoken_language=draft.spoken_language,
            segments=[
                ScriptSegment(sequence=index, **segment.model_dump())
                for index, segment in enumerate(draft.segments)
            ],
        )


def _validated_captions(
    captions: list[CaptionVariant],
    *,
    spoken_text: str,
    spoken_language: str,
) -> list[CaptionVariant]:
    if not captions:
        return [
            CaptionVariant(
                language=spoken_language,
                kind=CaptionKind.VERBATIM,
                text=spoken_text,
            )
        ]
    keys = [(caption.language.casefold(), caption.kind) for caption in captions]
    if len(keys) != len(set(keys)):
        raise ValueError("caption language/kind pairs must be unique")
    verbatim = [caption for caption in captions if caption.kind is CaptionKind.VERBATIM]
    if len(verbatim) != 1:
        raise ValueError("segments require exactly one verbatim caption")
    if (
        verbatim[0].language.casefold() != spoken_language.casefold()
        or verbatim[0].text != spoken_text
    ):
        raise ValueError("verbatim caption must exactly match spoken text and language")
    return captions


def _draft_with_document_language(
    segment: ScriptSegmentDraft,
    document_language: str,
) -> ScriptSegmentDraft:
    if segment.spoken_language != "und":
        return segment
    captions = [
        caption.model_copy(update={"language": document_language})
        if caption.language == "und"
        else caption
        for caption in segment.captions
    ]
    return segment.model_copy(
        update={"spoken_language": document_language, "captions": captions}
    )


def _segment_with_document_language(
    segment: ScriptSegment,
    document_language: str,
) -> ScriptSegment:
    if segment.spoken_language != "und":
        return segment
    captions = [
        caption.model_copy(update={"language": document_language})
        if caption.language == "und"
        else caption
        for caption in segment.captions
    ]
    return segment.model_copy(
        update={"spoken_language": document_language, "captions": captions}
    )


class AudioClip(DomainModel):
    segment_id: UUID
    path: NonBlankStr
    format: AudioFormat = AudioFormat.WAV
    duration_ms: int = Field(gt=0)
    sample_rate_hz: int = Field(gt=0)
    channels: int = Field(gt=0)
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class SegmentSynthesisProvenance(DomainModel):
    """The concrete voice assets selected for one rendered script segment.

    The bundle-level fields in :class:`SynthesisMetadata` describe the legacy
    single-voice case.  This record captures the real request-time selection
    once a script can switch speakers or emotion reference material per
    segment, without invalidating historical audio bundles.
    """

    segment_id: UUID
    speaker_id: NonBlankStr
    emotion: SpeechEmotion
    reference_audio_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    reference_text_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    gpt_weights_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    sovits_weights_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    model_identity: NonBlankStr
    # Roles may use reference material in different languages. Keep this at
    # the segment boundary rather than misrepresenting every clip with the
    # bundle-level, first-clip prompt language. ``und`` preserves readability
    # of the brief pre-field provenance format; new adapters must be explicit.
    prompt_language: NonBlankStr = "und"


class SynthesisMetadata(DomainModel):
    seed: int
    reference_audio_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    runtime_config_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    gpt_weights_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    sovits_weights_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    prompt_language: NonBlankStr
    text_language: NonBlankStr
    # Empty is intentionally valid for pre-multi-role bundles.  New adapters
    # should emit one entry per clip so the exact selected voice assets remain
    # auditable after profiles or references change on disk.
    segment_provenance: list[SegmentSynthesisProvenance] = Field(default_factory=list)


class AudioBundle(DomainModel):
    revision: int = Field(ge=1)
    provider: NonBlankStr
    model_identity: NonBlankStr
    synthesis: SynthesisMetadata
    generated_at: datetime = Field(default_factory=utc_now)
    clips: list[AudioClip] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_segments(self) -> AudioBundle:
        ids = [clip.segment_id for clip in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("audio clips must have unique segment_id values")
        provenance = self.synthesis.segment_provenance
        if provenance:
            provenance_ids = [entry.segment_id for entry in provenance]
            if len(provenance_ids) != len(set(provenance_ids)):
                raise ValueError("segment synthesis provenance must be unique per segment")
            if set(provenance_ids) != set(ids):
                raise ValueError(
                    "segment synthesis provenance must contain exactly one entry per audio clip"
                )
        return self


class TimelineSegment(DomainModel):
    segment_id: UUID
    sequence: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    spoken_text: NonBlankStr = Field(
        validation_alias=AliasChoices("spoken_text", "text")
    )
    spoken_language: NonBlankStr = "und"
    captions: list[CaptionVariant] = Field(default_factory=list, max_length=20)
    speaker_id: NonBlankStr
    emotion: SpeechEmotion
    scene_transition: SceneTransition = SceneTransition.BLACK
    visual_hint: str | None = None
    audio_path: NonBlankStr

    @field_validator("emotion", mode="before")
    @classmethod
    def normalize_legacy_emotion(cls, value: object) -> object:
        return normalize_legacy_speech_emotion(value)

    @model_validator(mode="after")
    def validate_time_range(self) -> TimelineSegment:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        self.captions = _validated_captions(
            self.captions,
            spoken_text=self.spoken_text,
            spoken_language=self.spoken_language,
        )
        return self

    @property
    def text(self) -> str:
        return self.spoken_text


class ProductionManifest(DomainModel):
    schema_version: Literal["1.0", "2.0"] = "2.0"
    story_id: UUID
    script_revision: int = Field(ge=1)
    spoken_language: NonBlankStr = Field(
        validation_alias=AliasChoices("spoken_language", "language")
    )
    total_duration_ms: int = Field(gt=0)
    timeline: list[TimelineSegment] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def apply_manifest_language(self) -> ProductionManifest:
        self.timeline = [
            segment.model_copy(
                update={
                    "spoken_language": self.spoken_language,
                    "captions": [
                        caption.model_copy(update={"language": self.spoken_language})
                        if caption.language == "und"
                        else caption
                        for caption in segment.captions
                    ],
                }
            )
            if segment.spoken_language == "und"
            else segment
            for segment in self.timeline
        ]
        return self

    @property
    def language(self) -> str:
        return self.spoken_language


class PipelineFailure(DomainModel):
    code: NonBlankStr
    message: NonBlankStr
    retryable: bool
    occurred_at: datetime = Field(default_factory=utc_now)


class Story(DomainModel):
    story_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    status: StoryStatus
    # This is the editable editorial headline.  ``source.title`` remains the
    # immutable title captured with the source evidence/provenance.
    title: NonBlankStr | None = None
    source: SourceSnapshot
    provenance: NormalizedSourceItem | None = None
    original_text: NonBlankStr
    target_language: NonBlankStr
    preferences: ScriptPreferences
    translation: TranslationResult | None = None
    script: ScriptDocument | None = None
    audio: AudioBundle | None = None
    last_failure: PipelineFailure | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_provenance_evidence(self) -> Story:
        if self.provenance is None:
            if self.source.kind is SourceKind.FIXED_SOURCE:
                raise ValueError("fixed-source stories require normalized provenance")
            return self
        if self.source.kind is not SourceKind.FIXED_SOURCE:
            raise ValueError("normalized provenance requires fixed_source kind")
        canonical_url = str(self.provenance.canonical_url)
        if self.source.final_uri != canonical_url or self.source.source_uri != canonical_url:
            raise ValueError("source snapshot URI must match normalized canonical URL")
        if self.source.content_sha256 != self.provenance.content_sha256:
            raise ValueError("source snapshot hash must match normalized provenance")
        if self.original_text != self.provenance.content_text:
            raise ValueError("original text must match normalized source content")
        return self

    @property
    def canonical_source_uri(self) -> str | None:
        if self.provenance is not None:
            return str(self.provenance.canonical_url)
        if self.source.kind is SourceKind.URL:
            return self.source.final_uri
        return None


class StoryUpdate(DomainModel):
    """Concurrent-safe edits to mutable editorial settings only.

    The source snapshot, normalized provenance, original text, target language,
    and voice controls intentionally are not part of this public write model.
    """

    expected_story_version: int = Field(ge=1)
    title: NonBlankStr | None = None
    style: NonBlankStr | None = None
    target_duration_seconds: int | None = Field(default=None, ge=5, le=600)

    @model_validator(mode="after")
    def require_edit(self) -> StoryUpdate:
        if self.title is None and self.style is None and self.target_duration_seconds is None:
            raise ValueError("at least one mutable story field must be supplied")
        return self


class RetranslateStoryRequest(DomainModel):
    """Optimistic-concurrency command for replacing an unreviewed translation."""

    expected_story_version: int = Field(ge=1)


class RegenerateSpokenSegmentRequest(DomainModel):
    """Generate one spoken-language segment from its human-editable caption."""

    expected_story_version: int = Field(ge=1)
    expected_script_revision: int = Field(ge=1)
    caption_text: NonBlankStr
    caption_language: NonBlankStr
    spoken_language: NonBlankStr


class RegeneratedSpokenSegment(DomainModel):
    segment_id: UUID
    spoken_text: NonBlankStr
    spoken_language: NonBlankStr


class ReviewRecord(DomainModel):
    review_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    stage: ReviewStage
    decision: ReviewDecision
    reviewer_id: NonBlankStr
    note: str | None = None
    reviewed_story_version: int = Field(ge=1)
    translation_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    script_revision: int | None = Field(default=None, ge=1)
    audio_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None
    request_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    ai_category: ContentCategory | None = None
    human_category: ContentCategory | None = None
    classification_accepted: bool | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_classification_audit(self) -> ReviewRecord:
        values = (self.ai_category, self.human_category, self.classification_accepted)
        if self.stage is ReviewStage.FIRST:
            if any(value is None for value in values):
                raise ValueError("first review requires classification audit fields")
        elif any(value is not None for value in values):
            raise ValueError("classification audit fields only belong to first review")
        return self


class FirstReviewSubmission(DomainModel):
    review_id: UUID = Field(default_factory=uuid4)
    expected_story_version: int = Field(ge=1)
    decision: ReviewDecision
    reviewer_id: NonBlankStr
    note: str | None = None
    corrected_title: NonBlankStr | None = None
    corrected_translation: str | None = None
    corrected_summary: str | None = None
    corrected_key_points: list[NonBlankStr] | None = Field(default=None, max_length=20)
    corrected_category: ContentCategory | None = None
    corrected_candidate_recommendation: bool | None = None
    preferences: ScriptPreferences | None = None

    @model_validator(mode="after")
    def require_change_details(self) -> FirstReviewSubmission:
        has_change = (
            any(
                (
                    self.note,
                    self.corrected_title,
                    self.corrected_translation,
                    self.corrected_summary,
                    self.corrected_key_points,
                    self.corrected_category,
                    self.preferences,
                )
            )
            or self.corrected_candidate_recommendation is not None
        )
        if self.decision is ReviewDecision.REQUEST_CHANGES and not has_change:
            raise ValueError("request_changes requires a note or correction")
        return self


class SecondReviewSubmission(DomainModel):
    review_id: UUID = Field(default_factory=uuid4)
    expected_story_version: int = Field(ge=1)
    decision: ReviewDecision
    reviewer_id: NonBlankStr
    note: str | None = None
    revised_script: ScriptDocument | None = None

    @model_validator(mode="after")
    def validate_revision(self) -> SecondReviewSubmission:
        if self.decision is ReviewDecision.APPROVE and self.revised_script is not None:
            raise ValueError("approve cannot include a revised_script")
        if self.decision is ReviewDecision.REQUEST_CHANGES and not (
            self.note or self.revised_script
        ):
            raise ValueError("request_changes requires a note or revised_script")
        return self


class ScriptReviewSubmission(DomainModel):
    """Human review of generated narration before any local TTS work begins."""

    review_id: UUID = Field(default_factory=uuid4)
    expected_story_version: int = Field(ge=1)
    decision: ReviewDecision
    reviewer_id: NonBlankStr
    note: str | None = None
    revised_script: ScriptDocument | None = None

    @model_validator(mode="after")
    def validate_revision(self) -> ScriptReviewSubmission:
        if self.decision is ReviewDecision.APPROVE and self.revised_script is not None:
            raise ValueError("approve cannot include a revised_script")
        if self.decision is ReviewDecision.REQUEST_CHANGES and not (
            self.note or self.revised_script
        ):
            raise ValueError("request_changes requires a note or revised_script")
        return self


class SynthesizeStoryRequest(DomainModel):
    """Explicit, optimistic-concurrency guarded command to start local TTS."""

    expected_story_version: int = Field(ge=1)


class StateTransition(DomainModel):
    transition_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    from_status: StoryStatus
    to_status: StoryStatus
    reason: NonBlankStr
    occurred_at: datetime = Field(default_factory=utc_now)


class MemoryQuery(DomainModel):
    query: NonBlankStr
    agent_id: NonBlankStr = "god-news-editor"
    limit: int = Field(default=5, ge=0, le=20)
    approved_only: bool = True


class MemoryItem(DomainModel):
    memory_id: NonBlankStr
    content: NonBlankStr
    score: float = Field(ge=0, le=1)
    category: str | None = None
    approved: bool


class MemoryWrite(DomainModel):
    content: NonBlankStr
    agent_id: NonBlankStr = "god-news-editor"
    category: NonBlankStr
    story_id: UUID
    approved: bool
    source_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class HealthReport(DomainModel):
    ready: bool
    checks: list[NonBlankStr]


class ClassificationMetrics(DomainModel):
    reviewed_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    accuracy: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_counts(self) -> ClassificationMetrics:
        if self.accepted_count > self.reviewed_count:
            raise ValueError("accepted_count cannot exceed reviewed_count")
        if (self.reviewed_count == 0) != (self.accuracy is None):
            raise ValueError("accuracy must be null exactly when no classifications were reviewed")
        return self
