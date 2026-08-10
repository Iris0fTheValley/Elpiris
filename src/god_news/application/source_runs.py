from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from god_news.domain.enums import StoryStatus
from god_news.domain.models import SourceItemIngestRequest
from god_news.errors import DuplicateStoryError, GodNewsError
from god_news.logging import reset_trace_id, set_trace_id
from god_news.sources.admission import SourceAdmissionPolicy
from god_news.sources.collectors.models import (
    CollectionErrorEvidence,
    CollectorDiagnostic,
    CollectorReadiness,
)
from god_news.sources.models import SourceName
from god_news.sources.protocols import SourceItemNormalizer
from god_news.sources.run_errors import SourceRunCapacityError
from god_news.sources.run_models import (
    SourceItemIngestionOutcome,
    SourceItemIngestionResult,
    SourceRun,
    SourceRunRequest,
    SourceRunStatus,
)
from god_news.sources.run_ports import (
    SourceCollectorGateway,
    SourceRunRepository,
    SourceStoryIngestor,
)

logger = logging.getLogger(__name__)


class SourceRunService:
    """Persisted orchestration around replaceable source collectors.

    HTTP starts a run and returns immediately. Collection and LLM ingestion run
    in a supervised task, while every material progress step is persisted.
    """

    def __init__(
        self,
        *,
        repository: SourceRunRepository,
        collectors: SourceCollectorGateway,
        normalizer: SourceItemNormalizer,
        admission_policy: SourceAdmissionPolicy,
        ingestor: SourceStoryIngestor,
        max_pending_runs: int = 8,
    ) -> None:
        if max_pending_runs < 1:
            raise ValueError("max_pending_runs must be positive")
        self._repository = repository
        self._collectors = collectors
        self._normalizer = normalizer
        self._admission_policy = admission_policy
        self._ingestor = ingestor
        self._max_pending_runs = max_pending_runs
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        # `Task.cancel()` carries no domain reason. Keep the operator intent
        # alongside the supervised task so persisted evidence distinguishes a
        # user cancellation from application shutdown.
        self._operator_cancellations: set[UUID] = set()
        self._task_lock = asyncio.Lock()
        self._closing = False

    def readiness(self) -> Sequence[CollectorReadiness]:
        return self._collectors.readiness()

    async def diagnose(self, source: SourceName) -> CollectorDiagnostic:
        return await self._collectors.diagnose(source)

    async def recover_interrupted(self) -> int:
        return await self._repository.recover_interrupted()

    async def start(self, request: SourceRunRequest, *, trace_id: UUID) -> SourceRun:
        async with self._task_lock:
            return await self._start_locked(request, trace_id=trace_id)

    async def start_if_source_idle(
        self,
        request: SourceRunRequest,
        *,
        trace_id: UUID,
    ) -> SourceRun | None:
        """Atomically avoid scheduler/manual overlap for one source."""

        async with self._task_lock:
            if await self._has_active_run(request.source):
                return None
            return await self._start_locked(request, trace_id=trace_id)

    async def active_runs(self) -> Sequence[SourceRun]:
        runs: list[SourceRun] = []
        for status in (
            SourceRunStatus.QUEUED,
            SourceRunStatus.COLLECTING,
            SourceRunStatus.INGESTING,
        ):
            runs.extend(
                await self._repository.list(
                    source=None,
                    status=status,
                    limit=self._max_pending_runs,
                    offset=0,
                )
            )
        return sorted(runs, key=lambda item: item.created_at)

    async def get(self, run_id: UUID) -> SourceRun:
        return await self._repository.get(run_id)

    async def wait(self, run_id: UUID) -> SourceRun:
        """Wait for an in-process run; already-finished runs return immediately."""
        async with self._task_lock:
            task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        return await self._repository.get(run_id)

    async def cancel(self, run_id: UUID) -> SourceRun:
        """Request cooperative cancellation and return the persisted terminal snapshot."""
        async with self._task_lock:
            task = self._tasks.get(run_id)
            if task is not None and not task.done():
                self._operator_cancellations.add(run_id)
                task.cancel()
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # The child task's cancellation is expected; the parent request
                # remains alive and can read the durable terminal record below.
                pass
        run = await self._repository.get(run_id)
        if run.status in {
            SourceRunStatus.COMPLETED,
            SourceRunStatus.FAILED,
            SourceRunStatus.CANCELLED,
        }:
            return run
        await self._mark_cancelled(run_id, operator_initiated=True)
        return await self._repository.get(run_id)

    async def list(
        self,
        *,
        source: SourceName | None,
        status: SourceRunStatus | None,
        limit: int,
        offset: int,
    ) -> Sequence[SourceRun]:
        return await self._repository.list(
            source=source,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def aclose(self) -> None:
        async with self._task_lock:
            self._closing = True
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._task_lock:
            self._tasks.clear()

    async def _start_locked(self, request: SourceRunRequest, *, trace_id: UUID) -> SourceRun:
        active = sum(not task.done() for task in self._tasks.values())
        if self._closing or active >= self._max_pending_runs:
            raise SourceRunCapacityError()
        run = await self._repository.create(SourceRun(trace_id=trace_id, request=request))
        task = asyncio.create_task(
            self._execute(run.run_id),
            name=f"god-news-source-run-{run.run_id}",
        )
        self._tasks[run.run_id] = task
        task.add_done_callback(partial(self._task_finished, run.run_id))
        return run

    async def _has_active_run(self, source: SourceName) -> bool:
        for status in (
            SourceRunStatus.QUEUED,
            SourceRunStatus.COLLECTING,
            SourceRunStatus.INGESTING,
        ):
            active = await self._repository.list(
                source=source,
                status=status,
                limit=1,
                offset=0,
            )
            if active:
                return True
        return False

    def _task_finished(self, run_id: UUID, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        self._operator_cancellations.discard(run_id)
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                logger.error(
                    "source run task escaped supervision run_id=%s error_type=%s",
                    run_id,
                    type(exception).__name__,
                )

    async def _execute(self, run_id: UUID) -> None:
        run = await self._repository.get(run_id)
        token = set_trace_id(str(run.trace_id))
        try:
            await self._execute_locked(run_id)
        except asyncio.CancelledError:
            await self._mark_cancelled(
                run_id,
                operator_initiated=run_id in self._operator_cancellations,
            )
            raise
        except Exception:
            logger.exception("source collection run failed run_id=%s", run_id)
            await self._mark_failed(
                run_id,
                CollectionErrorEvidence(
                    code="source_run_internal_error",
                    message="The source run failed unexpectedly; inspect trace-linked logs.",
                    retryable=False,
                ),
            )
        finally:
            reset_trace_id(token)

    async def _execute_locked(self, run_id: UUID) -> None:
        run = await self._repository.get(run_id)
        now = datetime.now(UTC)
        run = await self._save_update(
            run,
            status=SourceRunStatus.COLLECTING,
            started_at=now,
            updated_at=now,
        )
        logger.info("source collection started run_id=%s source=%s", run_id, run.request.source)
        collected = await self._collectors.collect(
            run.request.source,
            limit=run.request.limit,
        )
        run = await self._save_update(
            run,
            status=SourceRunStatus.INGESTING,
            collector_outcome=collected.outcome,
            attempts=list(collected.attempts),
            collection_errors=list(collected.errors),
            cooldown_wait_ms=collected.cooldown_wait_ms,
            items_discovered=len(collected.items),
            updated_at=datetime.now(UTC),
        )
        if not collected.items:
            error = collected.errors[0] if collected.errors else CollectionErrorEvidence(
                code="source_returned_no_items",
                message="The collector returned no items.",
                retryable=True,
            )
            await self._save_update(
                run,
                status=SourceRunStatus.FAILED,
                finished_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                run_error=error,
            )
            return

        for item in collected.items:
            normalized = self._normalizer.normalize(item)
            run = await self._save_update(
                run,
                current_item_index=len(run.item_results) + 1,
                current_external_id=normalized.external_id,
                current_title=normalized.title,
                current_url=self._sanitized_display_url(str(normalized.canonical_url)),
                updated_at=datetime.now(UTC),
            )
            admission = self._admission_policy.evaluate(normalized)
            if not admission.accepted:
                assert admission.error_code is not None
                logger.info(
                    "source item excluded before ingestion run_id=%s external_id=%s code=%s",
                    run_id,
                    normalized.external_id,
                    admission.error_code,
                )
                result = SourceItemIngestionResult(
                    external_id=normalized.external_id,
                    outcome=SourceItemIngestionOutcome.FILTERED,
                    error_code=admission.error_code,
                )
                run = await self._save_update(
                    run,
                    item_results=[*run.item_results, result],
                    updated_at=datetime.now(UTC),
                )
                continue
            request = SourceItemIngestRequest(
                item=item,
                target_language=run.request.target_language,
                style=run.request.style,
                target_duration_seconds=run.request.target_duration_seconds,
                speaker_id=run.request.speaker_id,
                emotion=run.request.emotion,
                speed=run.request.speed,
                pitch=run.request.pitch,
            )
            try:
                story = await self._ingestor.ingest_source_item(
                    request,
                    trace_id=run.trace_id,
                )
            except DuplicateStoryError as exc:
                result = SourceItemIngestionResult(
                    external_id=normalized.external_id,
                    outcome=SourceItemIngestionOutcome.DUPLICATE,
                    story_id=exc.story_id,
                    error_code=exc.code,
                )
            except GodNewsError as exc:
                result = SourceItemIngestionResult(
                    external_id=normalized.external_id,
                    outcome=SourceItemIngestionOutcome.FAILED,
                    story_id=exc.story_id,
                    error_code=exc.code,
                )
            except Exception:
                logger.exception(
                    "source item ingestion failed run_id=%s external_id=%s",
                    run_id,
                    normalized.external_id,
                )
                result = SourceItemIngestionResult(
                    external_id=normalized.external_id,
                    outcome=SourceItemIngestionOutcome.FAILED,
                    error_code="internal_error",
                )
            else:
                if story.status is StoryStatus.ARCHIVED:
                    result = SourceItemIngestionResult(
                        external_id=normalized.external_id,
                        outcome=SourceItemIngestionOutcome.FILTERED,
                        story_id=story.story_id,
                        error_code="editorial_not_recommended",
                    )
                else:
                    result = SourceItemIngestionResult(
                        external_id=normalized.external_id,
                        outcome=SourceItemIngestionOutcome.INGESTED,
                        story_id=story.story_id,
                    )
            run = await self._save_update(
                run,
                item_results=[*run.item_results, result],
                updated_at=datetime.now(UTC),
            )

        if run.item_results and all(
            result.outcome is SourceItemIngestionOutcome.FAILED
            for result in run.item_results
        ):
            await self._save_update(
                run,
                status=SourceRunStatus.FAILED,
                current_item_index=None,
                current_external_id=None,
                current_title=None,
                current_url=None,
                finished_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                run_error=CollectionErrorEvidence(
                    code="all_items_failed",
                    message="Every collected item failed during pipeline ingestion.",
                    retryable=True,
                ),
            )
            return
        await self._save_update(
            run,
            status=SourceRunStatus.COMPLETED,
            current_item_index=None,
            current_external_id=None,
            current_title=None,
            current_url=None,
            finished_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        logger.info("source collection completed run_id=%s", run_id)

    async def _mark_cancelled(self, run_id: UUID, *, operator_initiated: bool = False) -> None:
        try:
            run = await self._repository.get(run_id)
            if run.status in {
                SourceRunStatus.COMPLETED,
                SourceRunStatus.FAILED,
                SourceRunStatus.CANCELLED,
            }:
                return
            now = datetime.now(UTC)
            await self._save_update(
                run,
                status=SourceRunStatus.CANCELLED,
                current_item_index=None,
                current_external_id=None,
                current_title=None,
                current_url=None,
                started_at=run.started_at or now,
                finished_at=now,
                updated_at=now,
                run_error=CollectionErrorEvidence(
                    code=("operator_cancelled" if operator_initiated else "service_shutdown"),
                    message=(
                        "An operator cancelled this source run before it finished."
                        if operator_initiated
                        else "The service stopped before this source run finished."
                    ),
                    retryable=True,
                ),
            )
        except Exception:
            logger.exception("could not persist source run cancellation run_id=%s", run_id)

    async def _mark_failed(self, run_id: UUID, error: CollectionErrorEvidence) -> None:
        try:
            run = await self._repository.get(run_id)
            if run.status in {
                SourceRunStatus.COMPLETED,
                SourceRunStatus.FAILED,
                SourceRunStatus.CANCELLED,
            }:
                return
            now = datetime.now(UTC)
            await self._save_update(
                run,
                status=SourceRunStatus.FAILED,
                current_item_index=None,
                current_external_id=None,
                current_title=None,
                current_url=None,
                started_at=run.started_at or now,
                finished_at=now,
                updated_at=now,
                run_error=error,
            )
        except Exception:
            logger.exception("could not persist source run failure run_id=%s", run_id)

    async def _save_update(self, run: SourceRun, **updates: object) -> SourceRun:
        candidate = run.model_copy(update=updates)
        validated = SourceRun.model_validate(
            candidate.model_dump(exclude_computed_fields=True)
        )
        return await self._repository.save(validated, expected_version=run.version)

    @staticmethod
    def _sanitized_display_url(value: str) -> str:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
