from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from god_news.sources.models import (
    SOURCE_ORDER,
    ActiveSourceName,
    DazhongSourceFields,
    GuardianSourceFields,
    ImageMediaAsset,
    PikabuSourceFields,
    RawPikabuItem,
    RawRedditItem,
    RedditSourceFields,
    VideoMediaAsset,
    decode_windows_1251,
    parse_pikabu_windows_1251_json,
    parse_raw_source_json,
)
from god_news.sources.registry import SourceNormalizerRegistry, create_default_source_registry
from god_news.sources.text import content_sha256

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def test_active_news_sources_are_exactly_the_approved_four() -> None:
    approved = {"dazhong", "reddit", "guardian", "pikabu"}

    assert set(get_args(ActiveSourceName)) == approved
    assert set(SOURCE_ORDER) == approved


def test_real_pikabu_video_fixture_preserves_review_only_media_provenance() -> None:
    normalized = create_default_source_registry().normalize_json(
        (FIXTURES / "pikabu_video.json").read_bytes()
    )

    assert normalized.external_id == "pikabu:13922789"
    assert normalized.flags.has_video is True
    assert normalized.rights.status == "permission_required"
    assert normalized.rights.requires_human_review is True
    assert len(normalized.media) == 1
    video = normalized.media[0]
    assert isinstance(video, VideoMediaAsset)
    assert str(video.url) == (
        "https://cs16.pikabu.ru/s/2026/04/27/17/"
        "zoxx3sxj_s0f0d9m0_448x848.mp4"
    )
    assert video.duration_ms == 9_000


def fixture_bytes(source: str) -> bytes:
    return (FIXTURES / f"{source}.json").read_bytes()


@pytest.mark.parametrize(
    ("source", "expected_external_id"),
    [
        ("dazhong", "dazhong:NEWS3581000EHEVSBXUWQQSM"),
        ("reddit", "reddit:1u44n91"),
        (
            "guardian",
            "guardian:us-news/2026/jul/18/arizona-teen-rescue-woman-dementia-heatwave",
        ),
        ("pikabu", "pikabu:14207552"),
    ],
)
def test_each_fixture_has_typed_normalization_contract(
    source: str,
    expected_external_id: str,
) -> None:
    registry = create_default_source_registry()

    normalized = registry.normalize_json(fixture_bytes(source))

    assert normalized.source == source
    assert normalized.external_id == expected_external_id
    assert normalized.published_at.utcoffset() == timedelta(0)
    assert normalized.content_sha256 == content_sha256(normalized.content_text)
    assert normalized.attribution.source == source
    assert normalized.source_fields.source == source
    assert normalized.flags.requires_rights_review == normalized.rights.requires_human_review


def test_dazhong_nfkc_url_rights_and_media_contract() -> None:
    normalized = create_default_source_registry().normalize_json(fixture_bytes("dazhong"))

    assert normalized.title.startswith("开屏见“好”")
    assert "爱心餐从每天三十多份增加到近六十份" in normalized.content_text
    assert str(normalized.canonical_url) == (
        "https://m.dzplus.dzng.com/share/general/0/NEWS3581000EHEVSBXUWQQSM"
    )
    assert normalized.published_at.isoformat() == "2026-08-02T23:30:00+00:00"
    assert normalized.rights.requires_human_review is True
    assert isinstance(normalized.source_fields, DazhongSourceFields)
    assert normalized.source_fields.tags == ["好人好事", "爱心餐", "青岛"]
    assert normalized.media == []
    assert normalized.flags.has_images is False
    assert normalized.flags.has_video is False


def test_reddit_ignores_api_additions_and_uses_permalink_identity() -> None:
    raw = parse_raw_source_json(fixture_bytes("reddit"))

    assert isinstance(raw, RawRedditItem)
    assert "future_api_field" not in raw.model_fields_set
    assert raw.video is not None
    assert "bitrate_kbps" not in raw.video.model_fields_set

    normalized = create_default_source_registry().normalize(raw)
    assert normalized.title.startswith("Alright brother, you own that land now")
    assert str(normalized.canonical_url) == (
        "https://www.reddit.com/r/HumansBeingBros/comments/1u44n91/"
        "alright_brother_you_own_that_land_now_farmer/"
    )
    assert normalized.flags.is_user_generated is True
    assert normalized.flags.is_spoiler is False
    assert isinstance(normalized.source_fields, RedditSourceFields)
    assert str(normalized.source_fields.outbound_url) == "https://v.redd.it/urkrg4exhw6h1"
    assert {asset.kind for asset in normalized.media} == {"image", "video"}
    assert isinstance(normalized.media[0], ImageMediaAsset)
    assert isinstance(normalized.media[1], VideoMediaAsset)


def test_guardian_preserves_source_fields_and_extracts_media_union() -> None:
    normalized = create_default_source_registry().normalize_json(fixture_bytes("guardian"))

    assert normalized.title.startswith("Arizona teen hailed")
    assert normalized.rights.requires_human_review is True
    assert isinstance(normalized.source_fields, GuardianSourceFields)
    assert normalized.source_fields.section_id == "us-news"
    assert isinstance(normalized.media[0], ImageMediaAsset)
    assert normalized.flags.has_audio is False


def test_pikabu_block_union_and_windows_1251_boundary() -> None:
    utf8_payload = fixture_bytes("pikabu")
    legacy_payload = utf8_payload.decode("utf-8").replace("\u3000", " ").encode("windows-1251")

    raw = parse_pikabu_windows_1251_json(legacy_payload)

    assert isinstance(raw, RawPikabuItem)
    normalized = create_default_source_registry().normalize(raw)
    assert normalized.title.startswith("Сотрудница полиции")
    assert "лебедят нашли, отогрели" in normalized.content_text
    assert isinstance(normalized.source_fields, PikabuSourceFields)
    assert normalized.source_fields.tags == [
        "Доброта",
        "Позитив",
        "Помощь",
        "Россия",
        "Спасение животных",
    ]
    assert normalized.source_fields.block_count == 3
    assert {asset.kind for asset in normalized.media} == {"image"}


def test_windows_1251_decoder_fails_closed_on_undefined_byte() -> None:
    with pytest.raises(UnicodeDecodeError):
        decode_windows_1251(b"\x98")


def test_non_reddit_contracts_reject_unknown_fields() -> None:
    payload = (
        fixture_bytes("guardian")
        .decode("utf-8")
        .replace(
            '"publisher": "The Guardian",',
            '"publisher": "The Guardian", "undeclared": true,',
        )
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_raw_source_json(payload)


def test_normalizer_rejects_naive_source_timestamp() -> None:
    raw = parse_raw_source_json(fixture_bytes("pikabu"))
    assert isinstance(raw, RawPikabuItem)
    naive = raw.model_copy(update={"published_at": raw.published_at.replace(tzinfo=None)})

    with pytest.raises(ValueError, match="explicit timezone"):
        create_default_source_registry().normalize(naive)


def test_registry_rejects_duplicate_normalizer() -> None:
    registry = create_default_source_registry()
    from god_news.sources.normalizers.dazhong import DazhongSourceNormalizer

    with pytest.raises(ValueError, match="already registered"):
        registry.register(DazhongSourceNormalizer())

    assert registry.registered_sources == {
        "dazhong",
        "reddit",
        "guardian",
        "pikabu",
    }


def test_empty_registry_reports_missing_registration() -> None:
    raw = parse_raw_source_json(fixture_bytes("reddit"))

    with pytest.raises(LookupError, match="no normalizer"):
        SourceNormalizerRegistry().normalize(raw)
