from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from god_news.application.media_catalog import MediaCatalogService
from god_news.application.memory import MemoryCoordinator
from god_news.application.source_media import SourceMediaService
from god_news.application.source_runs import SourceRunService
from god_news.application.source_schedule import SourceCollectionScheduler
from god_news.application.source_transcriptions import SourceMediaTranscriptionService
from god_news.application.video_batches import VideoBatchService
from god_news.application.video_registry import VideoRegistryService
from god_news.application.visual_assets import VisualAssetService
from god_news.application.visual_discovery import VisualDiscoveryApplication
from god_news.application.workflow import StoryWorkflow
from god_news.config import MemoryProviderName, Settings
from god_news.domain.models import HealthReport
from god_news.domain.ports import (
    Fetcher,
    MemoryProvider,
    SpeechSynthesizer,
    StoryRepository,
    TextGenerator,
)
from god_news.domain.runtime_control_ports import RuntimeController
from god_news.domain.source_transcription import TimedCaptionTranslator
from god_news.domain.video_ports import HostRenderer, ProgramDirector
from god_news.domain.video_templates import create_default_template_registry
from god_news.infrastructure.database import Database
from god_news.infrastructure.fetchers.chain import FetcherChain
from god_news.infrastructure.fetchers.drission import DrissionPageFetcher
from god_news.infrastructure.fetchers.jina import JinaReaderFetcher
from god_news.infrastructure.fetchers.scrapy import ScrapyTrafilaturaFetcher
from god_news.infrastructure.fetchers.url_policy import UrlPolicy
from god_news.infrastructure.llm.openai_compatible import (
    OpenAICompatibleProgramDirector,
    OpenAICompatibleTextGenerator,
    OpenAICompatibleTimedCaptionTranslator,
    UnavailableProgramDirector,
    UnavailableTextGenerator,
    UnavailableTimedCaptionTranslator,
)
from god_news.infrastructure.media_catalog_repository import (
    SqlAlchemyMediaCatalogRepository,
)
from god_news.infrastructure.memory import ChromaDBMemoryProvider, NoopMemoryProvider
from god_news.infrastructure.repositories import (
    SqlAlchemyLiveScriptRoleUsageGuard,
    SqlAlchemyStoryRepository,
)
from god_news.infrastructure.role_profiles import SqlAlchemyRoleProfileRepository
from god_news.infrastructure.runtime_control import FileRuntimeController
from god_news.infrastructure.source_health import (
    HttpSourceReachabilityProbe,
    build_source_policies,
)
from god_news.infrastructure.source_media_asr import FasterWhisperSourceMediaTranscriber
from god_news.infrastructure.source_media_http import HttpSourceMediaDownloader
from god_news.infrastructure.source_media_probe import FFprobeSourceVideoInspector
from god_news.infrastructure.source_media_repository import SqlAlchemySourceMediaRepository
from god_news.infrastructure.source_media_store import LocalSourceMediaStore
from god_news.infrastructure.source_runs import SqlAlchemySourceRunRepository
from god_news.infrastructure.source_schedule import SqlAlchemySourceScheduleRepository
from god_news.infrastructure.source_transcription_repository import (
    SqlAlchemySourceTranscriptionRepository,
)
from god_news.infrastructure.tts.gpt_sovits import (
    GPTSoVITSSpeechSynthesizer,
    UnavailableSpeechSynthesizer,
)
from god_news.infrastructure.video_assets import LocalBgmCatalog
from god_news.infrastructure.video_broll_assets import ApprovedVisualDiscoveryBrollLibrary
from god_news.infrastructure.video_host import (
    PlaceholderHostRenderer,
    UnavailableBatchVideoRenderer,
)
from god_news.infrastructure.video_live2d import LocalLive2DHostRenderer
from god_news.infrastructure.video_registry_repository import (
    SqlAlchemyVideoCapabilityPolicyRepository,
)
from god_news.infrastructure.video_remotion import LocalRemotionBatchVideoRenderer
from god_news.infrastructure.video_repository import SqlAlchemyVideoBatchRepository
from god_news.infrastructure.video_source_assets import ApprovedSourceVideoAssetLibrary
from god_news.infrastructure.video_visual_assets import ApprovedVisualAssetLibrary
from god_news.infrastructure.visual_asset_store import LocalVisualAssetStore
from god_news.infrastructure.visual_discovery_repository import SqlAlchemyVisualDiscoveryRepository
from god_news.infrastructure.visual_discovery_store import LocalVisualDiscoveryStore
from god_news.infrastructure.visual_repository import SqlAlchemyVisualAssetRepository
from god_news.infrastructure.wikimedia_commons import WikimediaCommonsClient
from god_news.operations.retention import (
    CompositeRetentionAssetProtector,
    RetentionCleanupHandler,
)
from god_news.operations.roles import RoleProfileService
from god_news.operations.scheduler import IntervalScheduler, OperationDispatcher
from god_news.sources.admission import ContentAdmissionPolicy
from god_news.sources.collectors.factory import create_source_collectors
from god_news.sources.collectors.rate_limited import RateLimitedSourceCollectorGateway
from god_news.sources.health import SourceHealthMonitor
from god_news.sources.registry import SourceNormalizerRegistry, create_default_source_registry


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    repository: StoryRepository
    fetcher: Fetcher
    generator: TextGenerator
    memory_provider: MemoryProvider
    synthesizer: SpeechSynthesizer
    workflow: StoryWorkflow
    source_normalizers: SourceNormalizerRegistry
    source_health: SourceHealthMonitor
    source_runs: SourceRunService | None = None
    source_scheduler: SourceCollectionScheduler | None = None
    video_batches: VideoBatchService | None = None
    video_registry: VideoRegistryService | None = None
    role_profiles: RoleProfileService | None = None
    visual_assets: VisualAssetService | None = None
    visual_discovery: VisualDiscoveryApplication | None = None
    media_catalog: MediaCatalogService | None = None
    source_media: SourceMediaService | None = None
    source_transcriptions: SourceMediaTranscriptionService | None = None
    operations: OperationDispatcher | None = None
    operation_scheduler: IntervalScheduler | None = None
    database: Database | None = None
    http_client: httpx.AsyncClient | None = None
    runtime_control: RuntimeController | None = None

    async def start(self) -> None:
        if self.source_runs is not None:
            await self.source_runs.recover_interrupted()
        if self.source_transcriptions is not None:
            await self.source_transcriptions.recover_interrupted()
        if self.source_scheduler is not None:
            await self.source_scheduler.start()
        if self.video_batches is not None:
            await self.video_batches.recover_interrupted()
        if self.operation_scheduler is not None:
            await self.operation_scheduler.start()

    async def readiness(self) -> HealthReport:
        checks: list[str] = []
        ready = True
        try:
            await self.repository.healthcheck()
            checks.append("database:ok")
        except Exception:
            checks.append("database:error")
            ready = False

        if self.settings.active_llm_api_key is None:
            checks.append("llm:missing_api_key")
            ready = False
        else:
            try:
                await asyncio.wait_for(
                    self.generator.healthcheck(),
                    timeout=self.settings.readiness_timeout_seconds,
                )
                checks.append(f"llm:ok:{self.settings.llm_provider.value}")
            except Exception:
                checks.append(f"llm:error:{self.settings.llm_provider.value}")
                ready = False

        if self.settings.memory_provider is MemoryProviderName.NOOP:
            checks.append("memory:noop")
            if self.settings.environment.value == "production":
                ready = False
        else:
            try:
                await asyncio.wait_for(
                    self.memory_provider.healthcheck(),
                    timeout=self.settings.readiness_timeout_seconds,
                )
                checks.append("memory:ok:chromadb")
            except Exception:
                checks.append("memory:error:chromadb")
                ready = False

        if self.settings.enable_drission_fetcher and not self.settings.browser_egress_isolated:
            checks.append("fetcher:browser_egress_not_isolated")
            if self.settings.environment.value == "production":
                ready = False
        else:
            checks.append("fetcher:egress_policy_acknowledged")

        if not self.settings.source_media_asr_enabled:
            checks.append("asr:disabled")
        elif not FasterWhisperSourceMediaTranscriber.available():
            checks.append("asr:missing_optional_dependency")
            ready = False
        else:
            checks.append(
                f"asr:config_ok:{self.settings.source_media_asr_device.value}:"
                f"{self.settings.source_media_asr_compute_type.value}"
            )

        if not self.settings.tts_enabled:
            checks.append("tts:disabled")
            ready = False
        else:
            try:
                await asyncio.wait_for(
                    self.synthesizer.healthcheck(),
                    timeout=self.settings.readiness_timeout_seconds,
                )
                checks.append("tts:config_ok")
            except Exception:
                checks.append("tts:config_error")
                ready = False
        return HealthReport(ready=ready, checks=checks)

    async def aclose(self) -> None:
        if self.operation_scheduler is not None:
            await self.operation_scheduler.aclose()
        if self.source_scheduler is not None:
            await self.source_scheduler.aclose()
        if self.source_runs is not None:
            await self.source_runs.aclose()
        if self.source_transcriptions is not None:
            await self.source_transcriptions.aclose()
        await asyncio.gather(
            self.fetcher.aclose(),
            self.generator.aclose(),
            self.memory_provider.aclose(),
            self.synthesizer.aclose(),
            return_exceptions=True,
        )
        if self.http_client is not None:
            await self.http_client.aclose()
        if self.database is not None:
            await self.database.aclose()


async def build_container(settings: Settings) -> AppContainer:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.visual_asset_root.mkdir(parents=True, exist_ok=True)
    settings.visual_discovery_root.mkdir(parents=True, exist_ok=True)
    settings.source_media_root.mkdir(parents=True, exist_ok=True)
    settings.video_live2d_output_root.mkdir(parents=True, exist_ok=True)
    database = Database(
        settings.database_url,
        sqlite_busy_timeout_ms=settings.database_busy_timeout_ms,
    )
    if settings.database_auto_create:
        await database.create_schema()
    repository = SqlAlchemyStoryRepository(database.sessions)
    role_profile_repository = SqlAlchemyRoleProfileRepository(database.sessions)
    visual_asset_repository = SqlAlchemyVisualAssetRepository(
        database.sessions,
        storage_root=settings.visual_asset_root,
    )
    visual_discovery_repository = SqlAlchemyVisualDiscoveryRepository(
        database.sessions,
        storage_root=settings.visual_discovery_root,
    )
    source_media_repository = SqlAlchemySourceMediaRepository(
        database.sessions,
        storage_root=settings.source_media_root,
    )
    media_catalog_repository = SqlAlchemyMediaCatalogRepository(
        database.sessions,
        visual_root=settings.visual_asset_root,
        discovery_root=settings.visual_discovery_root,
        source_media_root=settings.source_media_root,
    )
    source_transcription_repository = SqlAlchemySourceTranscriptionRepository(database.sessions)
    live_script_role_usage_guard = SqlAlchemyLiveScriptRoleUsageGuard(database.sessions)
    role_profiles = RoleProfileService(role_profile_repository, live_script_role_usage_guard)
    asset_lifecycle_lock = asyncio.Lock()
    video_batch_repository = SqlAlchemyVideoBatchRepository(database.sessions)
    retention_handler = RetentionCleanupHandler(
        media_root=settings.output_dir,
        uploaded_mp4_root=settings.uploaded_video_dir,
        media_retention_days=settings.retention_media_days,
        uploaded_mp4_retention_days=settings.retention_uploaded_mp4_days,
        media_extensions=settings.retention_media_extensions,
        asset_protector=CompositeRetentionAssetProtector(
            video_batch_repository,
            visual_asset_repository,
            visual_discovery_repository,
            source_media_repository,
            media_catalog_repository,
        ),
        asset_lifecycle_lock=asset_lifecycle_lock,
    )
    operations = OperationDispatcher(
        [retention_handler],
        history_limit=settings.operations_history_limit,
    )
    operation_scheduler = IntervalScheduler(
        operations,
        enabled=settings.operations_scheduler_enabled,
        interval_seconds=settings.operations_scheduler_interval_seconds,
        poll_interval_seconds=settings.operations_scheduler_poll_seconds,
        retention_dry_run=settings.operations_scheduler_retention_dry_run,
    )

    timeout = httpx.Timeout(
        connect=settings.fetch_connect_timeout_seconds,
        read=settings.fetch_read_timeout_seconds,
        write=settings.fetch_connect_timeout_seconds,
        pool=settings.fetch_connect_timeout_seconds,
    )
    http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=settings.fetch_max_connections,
            max_keepalive_connections=settings.fetch_max_keepalive_connections,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    policy = UrlPolicy(
        allow_private=settings.allow_private_source_urls,
        allowed_ports=settings.allowed_source_ports,
    )
    url_fetchers: list[Fetcher] = [
        JinaReaderFetcher(
            client=http_client,
            policy=policy,
            base_url=settings.jina_base_url,
            api_key=(settings.jina_api_key.get_secret_value() if settings.jina_api_key else None),
            page_timeout_seconds=settings.jina_page_timeout_seconds,
            max_response_bytes=settings.fetch_max_response_bytes,
            min_content_characters=settings.fetch_min_content_characters,
        )
    ]
    if settings.enable_drission_fetcher:
        url_fetchers.append(
            DrissionPageFetcher(
                policy=policy,
                timeout_seconds=settings.drission_timeout_seconds,
                base_timeout_seconds=settings.drission_base_timeout_seconds,
                script_timeout_seconds=settings.drission_script_timeout_seconds,
                quit_timeout_seconds=settings.drission_quit_timeout_seconds,
                max_concurrency=settings.drission_max_concurrency,
                worker_module=settings.drission_worker_module,
                max_response_bytes=settings.fetch_max_response_bytes,
                min_content_characters=settings.fetch_min_content_characters,
            )
        )
    if settings.enable_scrapy_fetcher:
        url_fetchers.append(
            ScrapyTrafilaturaFetcher(
                policy=policy,
                timeout_seconds=settings.scrapy_timeout_seconds,
                worker_module=settings.scrapy_worker_module,
                max_response_bytes=settings.fetch_max_response_bytes,
                min_content_characters=settings.fetch_min_content_characters,
                download_timeout_seconds=settings.scrapy_download_timeout_seconds,
                redirect_max_times=settings.scrapy_redirect_max_times,
                retry_times=settings.scrapy_retry_times,
                depth_limit=settings.scrapy_depth_limit,
                close_page_count=settings.scrapy_close_page_count,
                user_agent=settings.scrapy_user_agent,
            )
        )
    fetcher = FetcherChain(url_fetchers)
    source_normalizers = create_default_source_registry()
    runtime_control = FileRuntimeController(
        command_path=settings.runtime_control_command_path,
        enabled=settings.runtime_control_enabled,
        supervised=settings.runtime_control_supervised,
    )
    source_health = SourceHealthMonitor(
        normalizers=source_normalizers,
        policies=build_source_policies(settings),
        probe=HttpSourceReachabilityProbe(http_client),
    )

    api_key = settings.active_llm_api_key
    if api_key is None:
        unavailable_llm_reason = "The selected LLM provider has no configured API key."
        caption_translator: TimedCaptionTranslator
        generator: TextGenerator = UnavailableTextGenerator(unavailable_llm_reason)
        program_director: ProgramDirector = UnavailableProgramDirector(unavailable_llm_reason)
        caption_translator = UnavailableTimedCaptionTranslator(unavailable_llm_reason)
    else:
        openai_generator = OpenAICompatibleTextGenerator(
            provider=settings.llm_provider,
            api_key=api_key.get_secret_value(),
            base_url=settings.active_llm_base_url,
            model=settings.active_llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            validation_retries=settings.llm_validation_retries,
            max_output_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
            response_cache_entries=settings.llm_response_cache_entries,
            max_source_characters=settings.max_source_characters,
            max_memory_characters=settings.memory_max_context_characters,
            thinking_enabled=settings.llm_thinking_enabled,
        )
        generator = openai_generator
        program_director = OpenAICompatibleProgramDirector(openai_generator)
        caption_translator = OpenAICompatibleTimedCaptionTranslator(openai_generator)

    if settings.memory_provider is MemoryProviderName.NOOP:
        memory_provider: MemoryProvider = NoopMemoryProvider()
    else:
        memory_provider = ChromaDBMemoryProvider(
            persist_directory=settings.memory_chroma_persist_directory,
            collection_name=settings.memory_chroma_collection,
            embedding_function_name=settings.memory_chroma_embedding_function.value,
            embedding_model_name=settings.memory_chroma_embedding_model.value,
        )
    memory = MemoryCoordinator(
        memory_provider,
        recall_fail_open=settings.memory_recall_fail_open,
        recall_limit=settings.memory_recall_limit,
    )

    if settings.tts_enabled:
        synthesizer: SpeechSynthesizer = GPTSoVITSSpeechSynthesizer(
            root=settings.gpt_sovits_root,
            python_executable=settings.gpt_sovits_python,
            source_config=settings.gpt_sovits_config,
            output_dir=settings.output_dir,
            reference_audio=settings.tts_reference_audio,
            reference_text_file=settings.tts_reference_text_file,
            prompt_language=settings.tts_prompt_language,
            text_language=settings.tts_text_language,
            default_speaker_id=settings.tts_default_speaker_id,
            model_profile=settings.tts_model_profile,
            gpt_weights=settings.tts_gpt_weights,
            sovits_weights=settings.tts_sovits_weights,
            device=settings.tts_device,
            use_half_precision=settings.tts_use_half_precision,
            startup_timeout_seconds=settings.tts_startup_timeout_seconds,
            request_timeout_seconds=settings.tts_request_timeout_seconds,
            shutdown_timeout_seconds=settings.tts_shutdown_timeout_seconds,
            probe_timeout_seconds=settings.tts_probe_timeout_seconds,
            startup_poll_interval_seconds=settings.tts_startup_poll_interval_seconds,
            force_shutdown_wait_seconds=settings.tts_force_shutdown_wait_seconds,
            process_kill_grace_seconds=settings.tts_process_kill_grace_seconds,
            drain_timeout_seconds=settings.tts_drain_timeout_seconds,
            max_audio_bytes=settings.tts_max_audio_bytes,
            seed=settings.tts_seed,
            top_k=settings.tts_top_k,
            top_p=settings.tts_top_p,
            temperature=settings.tts_temperature,
            text_split_method=settings.tts_text_split_method,
            batch_size=settings.tts_batch_size,
            batch_threshold=settings.tts_batch_threshold,
            fragment_interval=settings.tts_fragment_interval,
            repetition_penalty=settings.tts_repetition_penalty,
            parallel_infer=settings.tts_parallel_infer,
            max_concurrency=settings.tts_max_concurrency,
            voice_resolver=role_profiles,
            trusted_voice_asset_roots=settings.tts_trusted_asset_roots,
        )
    else:
        synthesizer = UnavailableSpeechSynthesizer("Local TTS is disabled by configuration.")

    workflow = StoryWorkflow(
        repository=repository,
        fetcher=fetcher,
        generator=generator,
        memory=memory,
        synthesizer=synthesizer,
        source_normalizer=source_normalizers,
        voice_resolver=role_profiles,
    )
    visual_asset_store = LocalVisualAssetStore(
        settings.visual_asset_root,
        max_upload_bytes=settings.visual_asset_max_upload_bytes,
        max_pixels=settings.visual_asset_max_pixels,
    )
    visual_assets = VisualAssetService(
        stories=repository,
        repository=visual_asset_repository,
        store=visual_asset_store,
        catalog_lifecycle=media_catalog_repository,
        asset_lifecycle_lock=asset_lifecycle_lock,
    )
    ffprobe = FFprobeSourceVideoInspector.discover(settings.video_remotion_package_dir.parent)
    visual_discovery_store = LocalVisualDiscoveryStore(
        settings.visual_discovery_root,
        max_download_bytes=settings.visual_discovery_max_download_bytes,
    )
    visual_discovery = VisualDiscoveryApplication(
        stories=repository,
        discovery=WikimediaCommonsClient(http_client, user_agent=settings.commons_user_agent),
        repository=visual_discovery_repository,
        store=visual_discovery_store,
        client=http_client,
        download_user_agent=settings.commons_user_agent,
        max_download_bytes=settings.visual_discovery_max_download_bytes,
        ffprobe_command=ffprobe,
    )
    source_media = SourceMediaService(
        stories=repository,
        repository=source_media_repository,
        store=LocalSourceMediaStore(
            settings.source_media_root,
            max_download_bytes=settings.source_media_max_download_bytes,
        ),
        downloader=HttpSourceMediaDownloader(
            http_client,
            policy,
            max_attempts=settings.source_media_download_attempts,
        ),
        inspector=(
            FFprobeSourceVideoInspector(
                ffprobe,
                timeout_seconds=settings.source_media_probe_timeout_seconds,
            )
            if ffprobe is not None
            else None
        ),
        asset_lifecycle_lock=asset_lifecycle_lock,
    )
    media_catalog = MediaCatalogService(
        repository=media_catalog_repository,
        asset_lifecycle_lock=asset_lifecycle_lock,
    )
    source_transcriber = (
        FasterWhisperSourceMediaTranscriber(
            model=settings.source_media_asr_model,
            device=settings.source_media_asr_device.value,
            compute_type=settings.source_media_asr_compute_type.value,
            download_root=settings.source_media_asr_model_cache_dir,
            local_files_only=settings.source_media_asr_local_files_only,
            timeout_seconds=settings.source_media_asr_timeout_seconds,
            max_output_bytes=settings.source_media_asr_max_output_bytes,
            cpu_threads=settings.source_media_asr_cpu_threads,
            beam_size=settings.source_media_asr_beam_size,
            vad_filter=settings.source_media_asr_vad_filter,
        )
        if settings.source_media_asr_enabled and FasterWhisperSourceMediaTranscriber.available()
        else None
    )
    source_transcriptions = SourceMediaTranscriptionService(
        stories=repository,
        media=source_media,
        repository=source_transcription_repository,
        transcriber=source_transcriber,
        translator=caption_translator,
        max_pending=settings.source_media_asr_max_pending,
    )
    source_runs = SourceRunService(
        repository=SqlAlchemySourceRunRepository(database.sessions),
        collectors=RateLimitedSourceCollectorGateway(
            create_source_collectors(
                settings=settings,
                client=http_client,
                fetcher=fetcher,
            )
        ),
        normalizer=source_normalizers,
        admission_policy=ContentAdmissionPolicy(),
        ingestor=workflow,
        max_pending_runs=settings.source_run_max_pending,
    )
    source_scheduler = SourceCollectionScheduler(
        repository=SqlAlchemySourceScheduleRepository(database.sessions),
        source_runs=source_runs,
        interval_seconds=settings.source_auto_collection_interval_seconds,
        poll_interval_seconds=settings.source_auto_collection_poll_seconds,
        initially_enabled=settings.source_auto_collection_initially_enabled,
        collection_limits={
            "dazhong": settings.source_dazhong_collection_limit,
            "reddit": settings.source_reddit_collection_limit,
            "guardian": settings.source_guardian_collection_limit,
            "pikabu": settings.source_pikabu_collection_limit,
        },
    )
    video_renderer = (
        LocalRemotionBatchVideoRenderer(
            package_dir=settings.video_remotion_package_dir,
            output_dir=settings.video_render_root,
            node_command=settings.video_node_command,
            quality_ffmpeg_command=settings.video_quality_ffmpeg_command,
            max_freeze_seconds=settings.video_quality_max_freeze_seconds,
            timeout_seconds=settings.video_render_timeout_seconds,
            max_parallel_batches=settings.video_render_max_parallel_batches,
            concurrency=settings.video_render_concurrency,
        )
        if settings.video_renderer_enabled
        else UnavailableBatchVideoRenderer()
    )
    host_renderer: HostRenderer
    if settings.video_live2d_enabled:
        if ffprobe is None or settings.video_live2d_python_executable is None:
            raise ValueError("Live2D rendering requires its configured Python runtime and ffprobe.")
        host_renderer = LocalLive2DHostRenderer(
            profiles=role_profiles,
            python_executable=settings.video_live2d_python_executable,
            worker_script=settings.video_live2d_worker_script,
            inspector=FFprobeSourceVideoInspector(
                ffprobe,
                timeout_seconds=settings.source_media_probe_timeout_seconds,
            ),
            output_root=settings.video_live2d_output_root,
            trusted_asset_roots=settings.video_live2d_trusted_asset_roots,
            timeout_seconds=settings.video_live2d_timeout_seconds,
            max_parallel_segments=settings.video_live2d_max_parallel_segments,
            width=settings.video_live2d_width,
            height=settings.video_live2d_height,
            fps=settings.video_live2d_fps,
            motion_policy=settings.video_live2d_motion_policy.value,
            sdk_auto_breath=settings.video_live2d_sdk_auto_breath,
            motion_intensity=settings.video_live2d_motion_intensity,
            mouth_attack_ms=settings.video_live2d_mouth_attack_ms,
            mouth_release_ms=settings.video_live2d_mouth_release_ms,
            seed=settings.video_live2d_seed,
            max_exact_duplicate_ratio=settings.video_live2d_max_exact_duplicate_ratio,
        )
    else:
        host_renderer = PlaceholderHostRenderer()
    template_registry = create_default_template_registry()
    video_registry = VideoRegistryService(
        templates=template_registry,
        policies=SqlAlchemyVideoCapabilityPolicyRepository(database.sessions),
        batches=video_batch_repository,
    )
    video_batches = VideoBatchService(
        story_pool=workflow,
        repository=video_batch_repository,
        host_renderer=host_renderer,
        program_director=program_director,
        synthesizer=synthesizer,
        video_renderer=video_renderer,
        bgm_catalog=LocalBgmCatalog(settings.video_bgm_directory),
        source_video_library=ApprovedSourceVideoAssetLibrary(
            media_repository=source_media_repository,
            transcription_repository=source_transcription_repository,
            media_reader=source_media,
            media_catalog=media_catalog_repository,
        ),
        broll_video_library=ApprovedVisualDiscoveryBrollLibrary(
            stories=repository,
            repository=visual_discovery_repository,
            store=visual_discovery_store,
            media_catalog=media_catalog_repository,
        ),
        visual_asset_library=ApprovedVisualAssetLibrary(
            repository=visual_asset_repository,
            store=visual_asset_store,
            discovery_repository=visual_discovery_repository,
            discovery_store=visual_discovery_store,
            media_catalog=media_catalog_repository,
        ),
        template_registry=template_registry,
        template_policy=video_registry,
        audio_root=settings.output_dir,
        candidate_scan_limit=settings.video_candidate_scan_limit,
        asset_lifecycle_lock=asset_lifecycle_lock,
    )
    return AppContainer(
        settings=settings,
        repository=repository,
        fetcher=fetcher,
        generator=generator,
        memory_provider=memory_provider,
        synthesizer=synthesizer,
        workflow=workflow,
        source_normalizers=source_normalizers,
        source_health=source_health,
        source_runs=source_runs,
        source_scheduler=source_scheduler,
        video_batches=video_batches,
        video_registry=video_registry,
        role_profiles=role_profiles,
        visual_assets=visual_assets,
        visual_discovery=visual_discovery,
        media_catalog=media_catalog,
        source_media=source_media,
        source_transcriptions=source_transcriptions,
        operations=operations,
        operation_scheduler=operation_scheduler,
        database=database,
        http_client=http_client,
        runtime_control=runtime_control,
    )
