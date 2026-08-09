from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse

from god_news.api.dependencies import get_container
from god_news.api.schemas import CreateStoryRequest, Liveness, ProblemDetail
from god_news.container import AppContainer
from god_news.domain.enums import StoryStatus
from god_news.domain.models import (
    ClassificationMetrics,
    FirstReviewSubmission,
    HealthReport,
    IngestRequest,
    ProductionManifest,
    RetranslateStoryRequest,
    ReviewRecord,
    ScriptReviewSubmission,
    SecondReviewSubmission,
    SourceItemIngestRequest,
    StateTransition,
    Story,
    StoryUpdate,
    SynthesizeStoryRequest,
)
from god_news.errors import ArtifactNotReadyError
from god_news.logging import trace_id_var
from god_news.sources.health import SourceHealthReport

PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ProblemDetail, "description": "Requested story was not found."},
    409: {"model": ProblemDetail, "description": "State or version conflict."},
    422: {"model": ProblemDetail, "description": "Request validation or source policy error."},
    500: {"model": ProblemDetail, "description": "Unexpected internal failure."},
    502: {"model": ProblemDetail, "description": "Fetcher, LLM, or TTS provider failure."},
    503: {"model": ProblemDetail, "description": "Required service is not configured."},
}
router = APIRouter(responses=PROBLEM_RESPONSES)
ContainerDependency = Annotated[AppContainer, Depends(get_container)]


@router.get(
    "/health/live",
    response_model=Liveness,
    operation_id="getLiveness",
    tags=["health"],
)
async def live() -> Liveness:
    return Liveness()


@router.get(
    "/health/ready",
    response_model=HealthReport,
    responses={503: {"model": HealthReport}},
    operation_id="getReadiness",
    tags=["health"],
)
async def ready(container: ContainerDependency) -> HealthReport | JSONResponse:
    report = await container.readiness()
    if report.ready:
        return report
    return JSONResponse(status_code=503, content=report.model_dump(mode="json"))


@router.get(
    "/sources/health",
    response_model=SourceHealthReport,
    operation_id="getSourceHealth",
    tags=["sources"],
)
async def source_health(
    container: ContainerDependency,
    probe_network: bool | None = None,
) -> SourceHealthReport:
    probes_enabled = container.settings.source_health_network_probes_enabled
    requested_probe = probes_enabled and (True if probe_network is None else probe_network)
    return await container.source_health.report(probe_network=requested_probe)


@router.get(
    "/metrics/classification",
    response_model=ClassificationMetrics,
    operation_id="getClassificationMetrics",
    tags=["metrics"],
)
async def classification_metrics(container: ContainerDependency) -> ClassificationMetrics:
    return await container.workflow.classification_metrics()


@router.post(
    "/stories",
    response_model=Story,
    status_code=201,
    operation_id="createStory",
    tags=["stories"],
)
async def ingest_story(request: CreateStoryRequest, container: ContainerDependency) -> Story:
    command = IngestRequest(source=request.source, target_language=request.target_language)
    return await container.workflow.ingest(command, trace_id=UUID(trace_id_var.get()))


@router.post(
    "/sources/items",
    response_model=Story,
    status_code=201,
    operation_id="createStoryFromSourceItem",
    tags=["sources", "stories"],
)
async def ingest_source_item(
    request: SourceItemIngestRequest,
    container: ContainerDependency,
) -> Story:
    return await container.workflow.ingest_source_item(
        request,
        trace_id=UUID(trace_id_var.get()),
    )


@router.get(
    "/stories",
    response_model=list[Story],
    operation_id="listStories",
    tags=["stories"],
)
async def list_stories(
    container: ContainerDependency,
    status: StoryStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_archived: bool = False,
) -> list[Story]:
    stories = await container.workflow.list(
        status=status,
        limit=limit,
        offset=offset,
        include_archived=include_archived,
    )
    return list(stories)


@router.get(
    "/stories/{story_id}",
    response_model=Story,
    operation_id="getStory",
    tags=["stories"],
)
async def get_story(story_id: UUID, container: ContainerDependency) -> Story:
    return await container.workflow.get(story_id)


@router.patch(
    "/stories/{story_id}",
    response_model=Story,
    operation_id="updateStory",
    tags=["stories"],
)
async def update_story(
    story_id: UUID,
    request: StoryUpdate,
    container: ContainerDependency,
) -> Story:
    return await container.workflow.update(story_id, request)


@router.delete(
    "/stories/{story_id}",
    response_model=Story,
    operation_id="archiveStory",
    tags=["stories"],
)
async def archive_story(story_id: UUID, container: ContainerDependency) -> Story:
    return await container.workflow.archive(story_id)


@router.post(
    "/stories/{story_id}/retranslate",
    response_model=Story,
    operation_id="retranslateStory",
    tags=["stories"],
)
async def retranslate_story(
    story_id: UUID,
    request: RetranslateStoryRequest,
    container: ContainerDependency,
) -> Story:
    return await container.workflow.retranslate(story_id, request)


@router.post(
    "/stories/{story_id}/reopen",
    response_model=Story,
    operation_id="reopenStory",
    tags=["stories"],
)
async def reopen_story(story_id: UUID, container: ContainerDependency) -> Story:
    return await container.workflow.reopen(story_id)


@router.post(
    "/stories/{story_id}/resume",
    response_model=Story,
    operation_id="resumeStory",
    tags=["stories"],
)
async def resume_story(story_id: UUID, container: ContainerDependency) -> Story:
    return await container.workflow.resume(story_id)


@router.post(
    "/stories/{story_id}/reviews/first",
    response_model=Story,
    operation_id="submitFirstReview",
    tags=["reviews"],
)
async def first_review(
    story_id: UUID,
    submission: FirstReviewSubmission,
    container: ContainerDependency,
) -> Story:
    return await container.workflow.submit_first_review(story_id, submission)


@router.post(
    "/stories/{story_id}/reviews/script",
    response_model=Story,
    operation_id="submitScriptReview",
    tags=["reviews"],
)
async def script_review(
    story_id: UUID,
    submission: ScriptReviewSubmission,
    container: ContainerDependency,
) -> Story:
    """Approve or return the human-editable narration before local TTS starts."""

    return await container.workflow.submit_script_review(story_id, submission)


@router.post(
    "/stories/{story_id}/synthesize",
    response_model=Story,
    operation_id="synthesizeStory",
    tags=["stories", "artifacts"],
)
async def synthesize_story(
    story_id: UUID,
    request: SynthesizeStoryRequest,
    container: ContainerDependency,
) -> Story:
    """Explicitly start isolated local TTS for an approved script revision."""

    return await container.workflow.synthesize(story_id, request)


@router.post(
    "/stories/{story_id}/reviews/second",
    response_model=Story,
    operation_id="submitSecondReview",
    tags=["reviews"],
)
async def second_review(
    story_id: UUID,
    submission: SecondReviewSubmission,
    container: ContainerDependency,
) -> Story:
    return await container.workflow.submit_second_review(story_id, submission)


@router.get(
    "/stories/{story_id}/reviews",
    response_model=list[ReviewRecord],
    operation_id="listStoryReviews",
    tags=["reviews"],
)
async def review_history(story_id: UUID, container: ContainerDependency) -> list[ReviewRecord]:
    return list(await container.workflow.reviews(story_id))


@router.get(
    "/stories/{story_id}/transitions",
    response_model=list[StateTransition],
    operation_id="listStoryTransitions",
    tags=["stories"],
)
async def transition_history(
    story_id: UUID,
    container: ContainerDependency,
) -> list[StateTransition]:
    return list(await container.workflow.transitions(story_id))


@router.get(
    "/stories/{story_id}/production-manifest",
    response_model=ProductionManifest,
    operation_id="getProductionManifest",
    tags=["artifacts"],
)
async def production_manifest(
    story_id: UUID,
    container: ContainerDependency,
) -> ProductionManifest:
    return await container.workflow.production_manifest(story_id)


@router.get(
    "/stories/{story_id}/audio/{segment_id}",
    response_class=FileResponse,
    operation_id="getAudioClip",
    tags=["artifacts"],
)
async def audio_clip(
    story_id: UUID,
    segment_id: UUID,
    container: ContainerDependency,
) -> FileResponse:
    story = await container.workflow.get(story_id)
    if story.audio is None:
        raise ArtifactNotReadyError(story_id, "Audio is not ready.")
    clip = next((item for item in story.audio.clips if item.segment_id == segment_id), None)
    if clip is None:
        raise ArtifactNotReadyError(story_id, "Audio segment does not exist.")
    root = container.settings.output_dir.resolve()
    path = (root / Path(clip.path)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ArtifactNotReadyError(story_id, "Audio path is invalid.") from exc
    if not path.is_file():
        raise ArtifactNotReadyError(story_id, "Audio file is missing.")
    return FileResponse(path, media_type="audio/wav", filename=path.name)
