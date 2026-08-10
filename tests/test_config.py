from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from god_news.config import Environment, MemoryProviderName, Settings


def test_blank_optional_secrets_and_weight_paths_are_unset(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="",
        jina_api_key="",
        tts_gpt_weights="",
        tts_sovits_weights="",
        output_dir=tmp_path,
    )
    assert settings.deepseek_api_key is None
    assert settings.llm_response_cache_entries == 256
    assert settings.llm_healthcheck_cache_seconds == 60
    assert settings.jina_api_key is None
    assert settings.tts_gpt_weights is None
    assert settings.tts_sovits_weights is None


def test_production_browser_requires_real_egress_isolation_flag() -> None:
    with pytest.raises(ValidationError, match="browser_egress_isolated"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            memory_provider=MemoryProviderName.CHROMADB,
            browser_egress_isolated=False,
        )


def test_example_environment_file_is_parseable() -> None:
    settings = Settings(_env_file=Path(".env.example"))
    assert settings.source_auto_collection_initially_enabled is False
    assert settings.source_auto_collection_interval_seconds >= 30
    assert settings.allowed_source_ports == (80, 443)
    assert settings.deepseek_api_key is None
    assert settings.tts_model_profile == "v2Pro"
    assert settings.memory_provider is MemoryProviderName.CHROMADB
    assert settings.memory_chroma_collection == "god-news-memory-v1"
    assert settings.memory_chroma_embedding_model.value == "all-MiniLM-L6-v2"
    assert settings.source_media_asr_enabled is False
    assert settings.source_media_asr_device.value == "cpu"
    assert settings.source_media_asr_compute_type.value == "int8"
    assert Path("J:/AI friend/DSakiko3.10").resolve() in settings.tts_trusted_asset_roots


def test_legacy_dazhong_root_is_upgraded_to_public_listing() -> None:
    settings = Settings(
        _env_file=None,
        source_dazhong_endpoint="https://m.dzplus.dzng.com/",
    )

    assert settings.source_dazhong_endpoint.endswith("/share/home/1/0")


def test_tts_trusted_asset_roots_must_not_be_empty(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="tts_trusted_asset_roots"):
        Settings(
            _env_file=None,
            output_dir=tmp_path,
            tts_trusted_asset_roots=(),
        )


@pytest.mark.parametrize("name", ["bad..name", "127.0.0.1"])
def test_chroma_collection_name_follows_upstream_constraints(name: str) -> None:
    with pytest.raises(ValidationError, match="memory_chroma_collection"):
        Settings(_env_file=None, memory_chroma_collection=name)


def test_asr_cache_cannot_target_a_filesystem_root() -> None:
    with pytest.raises(ValidationError, match="source_media_asr_model_cache_dir"):
        Settings(
            _env_file=None,
            source_media_asr_model_cache_dir=Path(Path.cwd().anchor),
        )


def test_live2d_requires_explicit_runtime_and_trusted_assets(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="video_live2d_python_executable"):
        Settings(
            _env_file=None,
            output_dir=tmp_path / "media",
            video_live2d_enabled=True,
        )

    with pytest.raises(ValidationError, match="video_live2d_trusted_asset_roots"):
        Settings(
            _env_file=None,
            output_dir=tmp_path / "media",
            video_live2d_enabled=True,
            video_live2d_python_executable=Path(sys.executable),
        )
