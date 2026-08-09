from __future__ import annotations

from pathlib import Path

from god_news.sources.admission import ContentAdmissionPolicy, guardian_query_with_exclusions
from god_news.sources.models import RawDazhongItem, RawGuardianItem
from god_news.sources.registry import create_default_source_registry

_FIXTURE = RawGuardianItem.model_validate_json(
    (Path(__file__).parent / "fixtures" / "sources" / "guardian.json").read_text(
        encoding="utf-8"
    )
)


def _normalized(**updates: object):  # type: ignore[no-untyped-def]
    raw = RawGuardianItem.model_validate(
        {
            **_FIXTURE.model_dump(mode="json"),
            **updates,
        }
    )
    return create_default_source_registry().normalize(raw)


def test_admission_rejects_excluded_sections_and_editorial_topics() -> None:
    policy = ContentAdmissionPolicy()

    politics = policy.evaluate(_normalized(section_id="politics"))
    sports = policy.evaluate(_normalized(section_id="football"))
    mixed_digest = policy.evaluate(
        _normalized(
            section_id="australia-news",
            web_title="Morning Mail: parliament debates while swimmers win gold",
        )
    )
    localized_sports_tag = policy.evaluate(
        _normalized(section_id="news", tags=["国际体育新闻"])
    )

    assert politics.error_code == "excluded_topic_politics"
    assert sports.error_code == "excluded_topic_sports"
    assert mixed_digest.error_code == "excluded_topic_politics"
    assert localized_sports_tag.error_code == "excluded_topic_sports"


def test_admission_accepts_benign_story_with_incidental_body_reference() -> None:
    policy = ContentAdmissionPolicy()
    item = _normalized(
        body_text=(
            "A retired football coach and her neighbours rebuilt a community library. "
            "The story is about the volunteer project, not a sporting event."
        )
    )

    assert policy.evaluate(item).accepted is True


def test_admission_does_not_treat_a_country_name_as_politics() -> None:
    policy = ContentAdmissionPolicy()
    item = _normalized(
        web_title="Volunteers in Israel rebuild a neighbourhood library",
        trail_text="Residents donated books and repaired the reading room.",
    )

    assert policy.evaluate(item).accepted is True


def test_dazhong_mixed_roundup_is_rejected_from_strong_body_markers() -> None:
    raw = RawDazhongItem.model_validate_json(
        (Path(__file__).parent / "fixtures" / "sources" / "dazhong.json").read_text(
            encoding="utf-8"
        )
    ).model_copy(
        update={
            "title": "今日早晚报",
            "body": "先看社区互助消息;随后是足球联赛赛况和两岸关系新闻。",
        }
    )
    item = create_default_source_registry().normalize(raw)

    decision = ContentAdmissionPolicy().evaluate(item)

    assert decision.error_code == "excluded_topic_politics"


def test_dazhong_benign_news_is_not_rejected_by_body_scan() -> None:
    raw = RawDazhongItem.model_validate_json(
        (Path(__file__).parent / "fixtures" / "sources" / "dazhong.json").read_text(
            encoding="utf-8"
        )
    )
    item = create_default_source_registry().normalize(raw)

    assert ContentAdmissionPolicy().evaluate(item).accepted is True


def test_guardian_query_adds_documented_negative_search_terms() -> None:
    query = guardian_query_with_exclusions("kindness")

    assert query.startswith("(kindness) AND NOT (")
    assert "politics" in query
    assert "football" in query
