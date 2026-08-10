from __future__ import annotations

import ipaddress
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LLMProvider(StrEnum):
    DEEPSEEK = "deepseek"
    LOCAL = "local"


class MemoryProviderName(StrEnum):
    CHROMADB = "chromadb"
    NOOP = "noop"


class ChromaEmbeddingFunctionName(StrEnum):
    """Embedding functions with an explicit, local execution contract."""

    DEFAULT = "default"


class Live2DMotionPolicy(StrEnum):
    IDLE = "idle"
    EMOTION_ONCE = "emotion_once"


class ChromaEmbeddingModelName(StrEnum):
    ALL_MINILM_L6_V2 = "all-MiniLM-L6-v2"


class ASRDevice(StrEnum):
    CPU = "cpu"
    CUDA = "cuda"


class ASRComputeType(StrEnum):
    INT8 = "int8"
    FLOAT16 = "float16"
    FLOAT32 = "float32"


class Settings(BaseSettings):
    """Single source of truth for mutable runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GOD_NEWS_",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "god-news"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    readiness_timeout_seconds: float = Field(default=10, gt=0, le=60)
    runtime_control_enabled: bool = False
    runtime_control_supervised: bool = False
    runtime_control_command_path: Path = Path("./data/dev-backend-command.json")
    database_url: str = "sqlite+aiosqlite:///./data/god_news.db"
    database_auto_create: bool = True
    database_busy_timeout_ms: int = Field(default=5_000, ge=0, le=60_000)
    output_dir: Path = Path("./outputs")
    # Visual assets intentionally live beneath output_dir so the existing
    # retention operation can discover them without walking arbitrary uploads.
    # None derives a deterministic child directory from output_dir.
    visual_asset_dir: Path | None = None
    visual_asset_max_upload_bytes: int = Field(
        default=15 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    visual_asset_max_pixels: int = Field(default=32_000_000, ge=1, le=100_000_000)
    visual_discovery_dir: Path | None = None
    visual_discovery_max_download_bytes: int = Field(default=250 * 1024 * 1024, ge=1024)
    commons_user_agent: str = (
        "god-news/0.1 (https://github.com/Iris0fTheValley/god-news; contact: repository issues)"
    )
    source_media_dir: Path | None = None
    source_media_max_download_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        le=2 * 1024 * 1024 * 1024,
    )
    source_media_probe_timeout_seconds: float = Field(default=30, gt=0, le=300)
    source_media_download_attempts: int = Field(default=4, ge=1, le=10)
    source_media_asr_enabled: bool = False
    source_media_asr_model: str = Field(
        default="base",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    source_media_asr_device: ASRDevice = ASRDevice.CPU
    source_media_asr_compute_type: ASRComputeType = ASRComputeType.INT8
    source_media_asr_model_cache_dir: Path = Path("./data/faster-whisper")
    source_media_asr_local_files_only: bool = False
    source_media_asr_timeout_seconds: float = Field(default=1_800, gt=0, le=7_200)
    source_media_asr_max_output_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1024,
        le=128 * 1024 * 1024,
    )
    source_media_asr_cpu_threads: int = Field(default=4, ge=1, le=64)
    source_media_asr_beam_size: int = Field(default=5, ge=1, le=20)
    source_media_asr_vad_filter: bool = True
    source_media_asr_max_pending: int = Field(default=2, ge=1, le=16)
    uploaded_video_dir: Path = Path("./uploads/videos")
    video_bgm_directory: Path = Path("./assets/bgm")
    video_candidate_scan_limit: int = Field(default=1_000, ge=15, le=100_000)
    video_renderer_enabled: bool = False
    video_remotion_package_dir: Path = Path("./video")
    video_render_output_dir: Path | None = None
    video_node_command: str = "node"
    video_quality_ffmpeg_command: str | None = None
    video_quality_max_freeze_seconds: float = Field(default=15.0, gt=1, le=60)
    video_render_timeout_seconds: float = Field(default=3_600, gt=0, le=14_400)
    video_render_max_parallel_batches: int = Field(default=1, ge=1, le=4)
    video_render_concurrency: int = Field(default=2, ge=1, le=8)
    video_live2d_enabled: bool = False
    video_live2d_python_executable: Path | None = None
    video_live2d_worker_script: Path = Path("./scripts/workers/render_live2d_host.py")
    video_live2d_trusted_asset_roots: tuple[Path, ...] = ()
    video_live2d_output_dir: Path | None = None
    video_live2d_timeout_seconds: float = Field(default=900, gt=0, le=7_200)
    video_live2d_max_parallel_segments: int = Field(default=1, ge=1, le=4)
    video_live2d_width: int = Field(default=720, ge=256, le=2_048, multiple_of=2)
    video_live2d_height: int = Field(default=720, ge=256, le=2_048, multiple_of=2)
    video_live2d_fps: int = Field(default=30, ge=1, le=60)
    video_live2d_motion_policy: Live2DMotionPolicy = Live2DMotionPolicy.IDLE
    video_live2d_sdk_auto_breath: bool = False
    video_live2d_motion_intensity: float = Field(default=0.35, ge=0, le=1)
    video_live2d_mouth_attack_ms: float = Field(default=55, gt=0, le=500)
    video_live2d_mouth_release_ms: float = Field(default=160, gt=0, le=1_000)
    video_live2d_seed: int = Field(default=20260717, ge=0, le=2**31 - 1)
    video_live2d_max_exact_duplicate_ratio: float = Field(default=0.15, ge=0, le=1)
    retention_media_days: int = Field(default=7, ge=1, le=3_650)
    retention_uploaded_mp4_days: int = Field(default=3, ge=1, le=3_650)
    retention_media_extensions: tuple[str, ...] = (
        ".wav",
        ".mp3",
        ".flac",
        ".aac",
        ".ogg",
        ".m4a",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".mp4",
        ".webm",
    )
    operations_history_limit: int = Field(default=100, ge=1, le=10_000)
    operations_scheduler_enabled: bool = False
    operations_scheduler_interval_seconds: float = Field(default=86_400, ge=60)
    operations_scheduler_poll_seconds: float = Field(default=5, gt=0, le=60)
    operations_scheduler_retention_dry_run: bool = True

    llm_provider: LLMProvider = LLMProvider.DEEPSEEK
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    local_llm_enabled: bool = False
    local_llm_base_url: str = "http://127.0.0.1:1234/v1"
    local_llm_api_key: SecretStr = SecretStr("lm-studio")
    local_llm_model: str = "Qwen3.5-35B-A3B-heretic-v2-Q4_K_M"
    llm_thinking_enabled: bool = False
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_validation_retries: int = Field(default=1, ge=0, le=2)
    llm_max_output_tokens: int = Field(default=4096, ge=256, le=32768)
    llm_temperature: float = Field(default=0.1, ge=0, le=2)
    llm_response_cache_entries: int = Field(default=256, ge=0, le=10_000)
    llm_healthcheck_cache_seconds: float = Field(default=60.0, ge=0, le=3_600)
    max_source_characters: int = Field(default=60_000, ge=1_000, le=500_000)

    memory_provider: MemoryProviderName = MemoryProviderName.CHROMADB
    memory_recall_fail_open: bool = True
    memory_recall_limit: int = Field(default=5, ge=0, le=20)
    memory_max_context_characters: int = Field(default=4_000, ge=0, le=20_000)
    memory_chroma_persist_directory: Path = Path("./data/chroma")
    memory_chroma_collection: str = Field(
        default="god-news-memory-v1",
        min_length=3,
        max_length=512,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$",
    )
    memory_chroma_embedding_function: ChromaEmbeddingFunctionName = (
        ChromaEmbeddingFunctionName.DEFAULT
    )
    memory_chroma_embedding_model: ChromaEmbeddingModelName = (
        ChromaEmbeddingModelName.ALL_MINILM_L6_V2
    )

    jina_api_key: SecretStr | None = None
    jina_base_url: str = "https://r.jina.ai"
    jina_page_timeout_seconds: int = Field(default=20, ge=1, le=180)
    fetch_connect_timeout_seconds: float = Field(default=10, gt=0, le=60)
    fetch_read_timeout_seconds: float = Field(default=45, gt=0, le=240)
    fetch_max_connections: int = Field(default=20, ge=1, le=1_000)
    fetch_max_keepalive_connections: int = Field(default=10, ge=0, le=1_000)
    fetch_max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    fetch_min_content_characters: int = Field(default=200, ge=1)
    allow_private_source_urls: bool = False
    allowed_source_ports: tuple[int, ...] = (80, 443)
    enable_drission_fetcher: bool = True
    drission_timeout_seconds: float = Field(default=35, gt=0, le=180)
    drission_base_timeout_seconds: float = Field(default=10, gt=0, le=60)
    drission_script_timeout_seconds: float = Field(default=10, gt=0, le=60)
    drission_quit_timeout_seconds: float = Field(default=5, gt=0, le=30)
    drission_max_concurrency: int = Field(default=1, ge=1, le=4)
    drission_worker_module: str = "god_news.workers.drission_fetch"
    browser_egress_isolated: bool = False
    enable_scrapy_fetcher: bool = True
    scrapy_timeout_seconds: float = Field(default=60, gt=0, le=300)
    scrapy_worker_module: str = "god_news.workers.scrapy_fetch"
    scrapy_download_timeout_seconds: int = Field(default=20, ge=1, le=180)
    scrapy_redirect_max_times: int = Field(default=3, ge=0, le=20)
    scrapy_retry_times: int = Field(default=1, ge=0, le=5)
    scrapy_depth_limit: int = Field(default=1, ge=1, le=10)
    scrapy_close_page_count: int = Field(default=3, ge=1, le=100)
    scrapy_user_agent: str = "god-news/0.1 (+content research pipeline)"

    # Fixed source adapter policies. Network-facing code may only target these
    # configured endpoints and must first map data into the typed raw contracts.
    source_health_network_probes_enabled: bool = False
    source_run_max_pending: int = Field(default=8, ge=1, le=100)
    # Operator policy only. These values are deliberately absent from the UI;
    # users may start/stop automation but cannot weaken collection cadence.
    source_auto_collection_initially_enabled: bool = False
    source_auto_collection_interval_seconds: float = Field(default=1_800, ge=30)
    source_auto_collection_poll_seconds: float = Field(default=5, gt=0, le=60)
    source_dazhong_enabled: bool = True
    source_dazhong_endpoint: str = "https://m.dzplus.dzng.com/share/home/1/0"
    source_dazhong_public_page_use_authorized: bool = False
    source_dazhong_collection_limit: int = Field(default=10, ge=1, le=50)
    source_dazhong_allowed_host_suffixes: tuple[str, ...] = ("dzng.com",)
    source_reddit_enabled: bool = True
    source_reddit_endpoint: str = "https://oauth.reddit.com/"
    source_reddit_token_endpoint: str = "https://www.reddit.com/api/v1/access_token"
    source_reddit_client_id: SecretStr | None = None
    source_reddit_client_secret: SecretStr | None = None
    source_reddit_user_agent: str | None = None
    source_reddit_api_use_authorized: bool = False
    source_reddit_subreddit: str = Field(
        default="HumansBeingBros",
        min_length=1,
        max_length=21,
        pattern=r"^[A-Za-z0-9_]+$",
    )
    source_reddit_collection_limit: int = Field(default=25, ge=1, le=100)
    source_guardian_enabled: bool = True
    source_guardian_endpoint: str = "https://content.guardianapis.com/"
    source_guardian_api_key: SecretStr | None = None
    source_guardian_ai_use_authorized: bool = False
    source_guardian_query: str = Field(default="kindness", min_length=1, max_length=200)
    source_guardian_section: str | None = Field(default=None, max_length=100)
    source_guardian_collection_limit: int = Field(default=25, ge=1, le=50)
    source_pikabu_enabled: bool = True
    source_pikabu_endpoint: str = "https://pikabu.ru/tag/%D0%94%D0%BE%D0%B1%D1%80%D0%BE%D1%82%D0%B0"
    source_pikabu_public_page_use_authorized: bool = False
    source_pikabu_collection_limit: int = Field(default=10, ge=1, le=50)
    source_pikabu_allowed_host_suffixes: tuple[str, ...] = ("pikabu.ru",)
    tts_enabled: bool = True
    gpt_sovits_root: Path = Path("J:/AI friend/GPT-SoVITS-v2pro-20250604")
    gpt_sovits_python: Path = Path("J:/AI friend/GPT-SoVITS-v2pro-20250604/runtime/python.exe")
    gpt_sovits_config: Path = Path(
        "J:/AI friend/GPT-SoVITS-v2pro-20250604/GPT_SoVITS/configs/tts_infer.yaml"
    )
    tts_reference_audio: Path = Path("J:/AI friend/GPT-SoVITS-v2pro-20250604/show/参考音频.wav")
    tts_reference_text_file: Path = Path("J:/AI friend/GPT-SoVITS-v2pro-20250604/show/参考文本.txt")
    tts_prompt_language: str = "en"
    tts_text_language: str = "auto"
    tts_default_speaker_id: str = "narrator"
    # Operator-controlled allowlist for reusable local voice assets. The
    # DSakiko location is only a convenient local default/example; deployments
    # should override this with their own trusted roots in .env.
    tts_trusted_asset_roots: tuple[Path, ...] = (
        Path("J:/AI friend/GPT-SoVITS-v2pro-20250604"),
        Path("J:/AI friend/DSakiko3.10"),
    )
    tts_model_profile: str = "v2Pro"
    tts_gpt_weights: Path | None = None
    tts_sovits_weights: Path | None = None
    tts_device: str = "cuda"
    tts_use_half_precision: bool = True
    tts_startup_timeout_seconds: float = Field(default=180, gt=0, le=900)
    tts_request_timeout_seconds: float = Field(default=300, gt=0, le=1800)
    tts_shutdown_timeout_seconds: float = Field(default=15, gt=0, le=60)
    tts_probe_timeout_seconds: float = Field(default=2, gt=0, le=30)
    tts_startup_poll_interval_seconds: float = Field(default=0.5, gt=0, le=10)
    tts_force_shutdown_wait_seconds: float = Field(default=0.5, gt=0, le=10)
    tts_process_kill_grace_seconds: float = Field(default=5, gt=0, le=30)
    tts_drain_timeout_seconds: float = Field(default=5, gt=0, le=30)
    tts_max_concurrency: int = Field(default=1, ge=1, le=1)
    tts_max_audio_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)
    tts_seed: int = Field(default=42, ge=-1, le=2_147_483_647)
    tts_top_k: int = Field(default=5, ge=1, le=100)
    tts_top_p: float = Field(default=1.0, gt=0, le=1)
    tts_temperature: float = Field(default=1.0, gt=0, le=2)
    tts_text_split_method: str = "cut5"
    tts_batch_size: int = Field(default=1, ge=1, le=20)
    tts_batch_threshold: float = Field(default=0.75, ge=0, le=1)
    tts_fragment_interval: float = Field(default=0.3, ge=0, le=5)
    tts_repetition_penalty: float = Field(default=1.35, ge=0.1, le=2)
    tts_parallel_infer: bool = True

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return normalized

    @field_validator("commons_user_agent")
    @classmethod
    def validate_commons_user_agent(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 500 or "\r" in normalized or "\n" in normalized:
            raise ValueError("commons_user_agent must be a single non-blank HTTP header value")
        return normalized

    @field_validator("allowed_source_ports")
    @classmethod
    def validate_ports(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(port < 1 or port > 65535 for port in value):
            raise ValueError("allowed_source_ports must contain valid TCP ports")
        return value

    @field_validator("retention_media_extensions")
    @classmethod
    def validate_retention_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(suffix.strip().casefold() for suffix in value)
        if (
            not normalized
            or len(normalized) != len(set(normalized))
            or any(
                len(suffix) < 2 or not suffix.startswith(".") or not suffix[1:].isalnum()
                for suffix in normalized
            )
        ):
            raise ValueError("retention_media_extensions must be unique file suffixes")
        return normalized

    @field_validator("memory_chroma_collection")
    @classmethod
    def validate_chroma_collection_name(cls, value: str) -> str:
        if ".." in value:
            raise ValueError("memory_chroma_collection cannot contain consecutive periods")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return value
        raise ValueError("memory_chroma_collection cannot be an IP address")

    @field_validator(
        "deepseek_api_key",
        "jina_api_key",
        "source_reddit_client_id",
        "source_reddit_client_secret",
        "source_guardian_api_key",
        mode="before",
    )
    @classmethod
    def blank_secret_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("source_reddit_user_agent", mode="before")
    @classmethod
    def blank_string_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("source_guardian_section", mode="before")
    @classmethod
    def blank_section_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "source_dazhong_allowed_host_suffixes",
        "source_pikabu_allowed_host_suffixes",
    )
    @classmethod
    def validate_source_host_suffixes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(suffix.casefold().strip().lstrip(".") for suffix in value)
        if not normalized or any(not suffix or "/" in suffix for suffix in normalized):
            raise ValueError("source host suffix allowlists must contain DNS suffixes")
        return normalized

    @field_validator("source_dazhong_endpoint", mode="before")
    @classmethod
    def upgrade_legacy_dazhong_landing_page(cls, value: object) -> object:
        if (
            isinstance(value, str)
            and value.strip().rstrip("/") == "https://m.dzplus.dzng.com"
        ):
            return "https://m.dzplus.dzng.com/share/home/1/0"
        return value

    @field_validator(
        "source_dazhong_endpoint",
        "source_reddit_endpoint",
        "source_reddit_token_endpoint",
        "source_guardian_endpoint",
        "source_pikabu_endpoint",
    )
    @classmethod
    def require_https_source_endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("fixed source endpoints must be absolute HTTPS URLs")
        if parts.username is not None or parts.password is not None:
            raise ValueError("fixed source endpoints cannot contain credentials")
        return value

    @model_validator(mode="after")
    def require_official_source_hosts(self) -> Settings:
        endpoints = {
            "source_reddit_endpoint": (self.source_reddit_endpoint, {"oauth.reddit.com"}),
            "source_reddit_token_endpoint": (
                self.source_reddit_token_endpoint,
                {"www.reddit.com"},
            ),
            "source_guardian_endpoint": (
                self.source_guardian_endpoint,
                {"content.guardianapis.com"},
            ),
        }
        for field_name, (value, allowed_hosts) in endpoints.items():
            host = (urlsplit(value).hostname or "").casefold()
            if host not in allowed_hosts:
                raise ValueError(f"{field_name} must use its official provider host")
        public_pages = {
            "source_dazhong_endpoint": (
                self.source_dazhong_endpoint,
                self.source_dazhong_allowed_host_suffixes,
            ),
            "source_pikabu_endpoint": (
                self.source_pikabu_endpoint,
                self.source_pikabu_allowed_host_suffixes,
            ),
        }
        for field_name, (value, suffixes) in public_pages.items():
            host = (urlsplit(value).hostname or "").casefold()
            if not any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
                raise ValueError(f"{field_name} must match its configured host allowlist")
        return self

    @field_validator(
        "tts_gpt_weights",
        "tts_sovits_weights",
        "visual_asset_dir",
        "visual_discovery_dir",
        "source_media_dir",
        "video_render_output_dir",
        "video_live2d_python_executable",
        "video_live2d_output_dir",
        mode="before",
    )
    @classmethod
    def blank_path_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("tts_model_profile")
    @classmethod
    def require_profile_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tts_model_profile cannot be blank")
        return value.strip()

    @field_validator("video_node_command")
    @classmethod
    def require_video_node_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("video_node_command cannot be blank")
        return value.strip()

    @field_validator("video_live2d_trusted_asset_roots")
    @classmethod
    def validate_live2d_trusted_asset_roots(
        cls,
        value: tuple[Path, ...],
    ) -> tuple[Path, ...]:
        roots = tuple(path.expanduser().resolve(strict=False) for path in value)
        if len(roots) != len(set(roots)):
            raise ValueError("video_live2d_trusted_asset_roots must not contain duplicates")
        if any(root.parent == root for root in roots):
            raise ValueError("video_live2d_trusted_asset_roots cannot include a filesystem root")
        return roots

    @field_validator("tts_trusted_asset_roots")
    @classmethod
    def validate_tts_trusted_asset_roots(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        roots = tuple(path.expanduser().resolve(strict=False) for path in value)
        if not roots:
            raise ValueError("tts_trusted_asset_roots cannot be empty")
        if len(roots) != len(set(roots)):
            raise ValueError("tts_trusted_asset_roots must not contain duplicates")
        if any(root.parent == root for root in roots):
            raise ValueError("tts_trusted_asset_roots cannot include a filesystem root")
        return roots

    @field_validator("source_media_asr_model_cache_dir")
    @classmethod
    def validate_asr_model_cache_dir(cls, value: Path) -> Path:
        root = value.expanduser().resolve(strict=False)
        if root.parent == root:
            raise ValueError("source_media_asr_model_cache_dir cannot be a filesystem root")
        return root

    @model_validator(mode="after")
    def validate_provider_selection(self) -> Settings:
        if self.llm_provider is LLMProvider.LOCAL and not self.local_llm_enabled:
            raise ValueError("local_llm_enabled must be true when llm_provider=local")
        if (
            self.environment is Environment.PRODUCTION
            and self.memory_provider is MemoryProviderName.NOOP
        ):
            raise ValueError("production requires a configured memory provider")
        if (self.tts_gpt_weights is None) is not (self.tts_sovits_weights is None):
            raise ValueError("tts_gpt_weights and tts_sovits_weights must be set together")
        if self.video_live2d_enabled:
            if self.video_live2d_python_executable is None:
                raise ValueError(
                    "video_live2d_python_executable is required when Live2D is enabled"
                )
            if not self.video_live2d_trusted_asset_roots:
                raise ValueError(
                    "video_live2d_trusted_asset_roots is required when Live2D is enabled"
                )
        if self.fetch_max_keepalive_connections > self.fetch_max_connections:
            raise ValueError("fetch_max_keepalive_connections cannot exceed fetch_max_connections")
        media_root = self.output_dir.expanduser().resolve(strict=False)
        visual_asset_root = self.visual_asset_root
        visual_discovery_root = self.visual_discovery_root
        source_media_root = self.source_media_root
        upload_root = self.uploaded_video_dir.expanduser().resolve(strict=False)
        workspace_root = Path.cwd().resolve()
        for field_name, root in {
            "output_dir": media_root,
            "uploaded_video_dir": upload_root,
        }.items():
            if root.parent == root:
                raise ValueError(f"{field_name} cannot be a filesystem root")
            if root == workspace_root or root in workspace_root.parents:
                raise ValueError(
                    f"{field_name} must be a child of the workspace or another data directory"
                )
        if (
            media_root == upload_root
            or media_root in upload_root.parents
            or upload_root in media_root.parents
        ):
            raise ValueError("output_dir and uploaded_video_dir must not overlap")
        if visual_asset_root == media_root or not visual_asset_root.is_relative_to(media_root):
            raise ValueError("visual_asset_dir must be a child directory of output_dir")
        if visual_discovery_root == media_root or not visual_discovery_root.is_relative_to(
            media_root
        ):
            raise ValueError("visual_discovery_dir must be a child directory of output_dir")
        if source_media_root == media_root or not source_media_root.is_relative_to(media_root):
            raise ValueError("source_media_dir must be a child directory of output_dir")
        if source_media_root == visual_asset_root:
            raise ValueError("source_media_dir and visual_asset_dir must be different")
        if visual_discovery_root in {source_media_root, visual_asset_root}:
            raise ValueError("visual_discovery_dir must be separate from source and editor media")
        video_render_root = self.video_render_root
        if video_render_root == media_root or not video_render_root.is_relative_to(media_root):
            raise ValueError("video_render_output_dir must be a child directory of output_dir")
        live2d_output_root = self.video_live2d_output_root
        if live2d_output_root == media_root or not live2d_output_root.is_relative_to(media_root):
            raise ValueError("video_live2d_output_dir must be a child directory of output_dir")
        if live2d_output_root == video_render_root:
            raise ValueError("video Live2D and final render output directories must differ")
        if (
            self.environment is Environment.PRODUCTION
            and self.enable_drission_fetcher
            and not self.browser_egress_isolated
        ):
            raise ValueError(
                "production DrissionPage requires browser_egress_isolated=true after an "
                "OS/container egress policy is actually installed"
            )
        return self

    @property
    def active_llm_base_url(self) -> str:
        if self.llm_provider is LLMProvider.LOCAL:
            return self.local_llm_base_url
        return self.deepseek_base_url

    @property
    def visual_asset_root(self) -> Path:
        """Resolved local root for image assets; kept inside the media lifecycle root."""

        configured = self.visual_asset_dir
        root = configured if configured is not None else self.output_dir / "visual-assets"
        return root.expanduser().resolve(strict=False)

    @property
    def source_media_root(self) -> Path:
        configured = self.source_media_dir
        root = configured if configured is not None else self.output_dir / "source-media"
        return root.expanduser().resolve(strict=False)

    @property
    def visual_discovery_root(self) -> Path:
        configured = self.visual_discovery_dir
        root = configured if configured is not None else self.output_dir / "visual-discovery"
        return root.expanduser().resolve(strict=False)

    @property
    def video_render_root(self) -> Path:
        configured = self.video_render_output_dir
        root = configured if configured is not None else self.output_dir / "video-renders"
        return root.expanduser().resolve(strict=False)

    @property
    def video_live2d_output_root(self) -> Path:
        configured = self.video_live2d_output_dir
        root = configured if configured is not None else self.output_dir / "live2d-hosts"
        return root.expanduser().resolve(strict=False)

    @property
    def active_llm_model(self) -> str:
        if self.llm_provider is LLMProvider.LOCAL:
            return self.local_llm_model
        return self.deepseek_model

    @property
    def active_llm_api_key(self) -> SecretStr | None:
        if self.llm_provider is LLMProvider.LOCAL:
            return self.local_llm_api_key
        return self.deepseek_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
