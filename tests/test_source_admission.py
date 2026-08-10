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
    local_government = policy.evaluate(
        _normalized(
            section_id="news",
            web_title="推动新就业群体融入基层治理",
        )
    )

    assert politics.error_code == "excluded_topic_politics"
    assert sports.error_code == "excluded_topic_sports"
    assert mixed_digest.error_code == "excluded_topic_politics"
    assert localized_sports_tag.error_code == "excluded_topic_sports"
    assert local_government.error_code == "excluded_topic_politics"


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


def test_dazhong_rejects_real_world_military_and_sports_headlines() -> None:
    base = RawDazhongItem.model_validate_json(
        (Path(__file__).parent / "fixtures" / "sources" / "dazhong.json").read_text(
            encoding="utf-8"
        )
    )
    registry = create_default_source_registry()
    military = registry.normalize(
        base.model_copy(
            update={
                "title": "美国导弹库存亮起红灯 白宫与五角大楼如何破局",
                "body": "美国国防部讨论武器弹药库存和军事冲突。",
            }
        )
    )
    sports = registry.normalize(
        base.model_copy(
            update={
                "title": "齐鲁超赛第十三轮五佳球由你定",
                "body": "常规赛五场比赛产生十四粒进球,球迷可投票。",
            }
        )
    )

    assert ContentAdmissionPolicy().evaluate(military).error_code == "excluded_topic_politics"
    assert ContentAdmissionPolicy().evaluate(sports).error_code == "excluded_topic_sports"


def test_admission_rejects_disaster_adoption_corruption_and_routine_technology() -> None:
    policy = ContentAdmissionPolicy()
    cases = {
        "A typhoon warning is in force after severe flooding": "excluded_topic_disaster",
        "Friendly rescue dog is looking for a forever home": "excluded_topic_adoption",
        "Former official convicted of accepting bribes": "excluded_topic_corruption",
        "New artificial intelligence platform launches for retailers": (
            "excluded_topic_minor_technology"
        ),
    }

    for title, expected_code in cases.items():
        assert policy.evaluate(_normalized(web_title=title)).error_code == expected_code


def test_admission_allows_only_explicitly_major_technology_achievement() -> None:
    policy = ContentAdmissionPolicy()
    major = _normalized(
        web_title="World-first medical technology marks a major breakthrough",
        trail_text="Researchers published independently reviewed results.",
    )
    routine = _normalized(
        web_title="Agricultural technology platform expands to another district",
    )
    dazhong_base = RawDazhongItem.model_validate_json(
        (Path(__file__).parent / "fixtures" / "sources" / "dazhong.json").read_text(
            encoding="utf-8"
        )
    )
    major_chinese = create_default_source_registry().normalize(
        dazhong_base.model_copy(
            update={
                "title": "新矿物+1!我国首个,正式获批",
                "body": "我国科研团队确认一种新矿物,形成重大科学发现。",
            }
        )
    )

    assert policy.evaluate(major).accepted is True
    assert policy.evaluate(major_chinese).accepted is True
    assert policy.evaluate(routine).error_code == "excluded_topic_minor_technology"


def test_dazhong_body_scan_rejects_generic_disaster_and_corruption_roundups() -> None:
    base = RawDazhongItem.model_validate_json(
        (Path(__file__).parent / "fixtures" / "sources" / "dazhong.json").read_text(
            encoding="utf-8"
        )
    )
    registry = create_default_source_registry()
    disaster = registry.normalize(
        base.model_copy(update={"title": "今日简报", "body": "多地启动洪水防御响应。"})
    )
    corruption = registry.normalize(
        base.model_copy(update={"title": "今日简报", "body": "某干部因受贿被判刑。"})
    )

    assert ContentAdmissionPolicy().evaluate(disaster).error_code == "excluded_topic_disaster"
    assert ContentAdmissionPolicy().evaluate(corruption).error_code == "excluded_topic_corruption"


def test_dazhong_body_scan_rejects_routine_agricultural_technology() -> None:
    base = RawDazhongItem.model_validate_json(
        (Path(__file__).parent / "fixtures" / "sources" / "dazhong.json").read_text(
            encoding="utf-8"
        )
    )
    item = create_default_source_registry().normalize(
        base.model_copy(
            update={
                "title": "来自田间的三本账",
                "body": "当地扩大水肥一体化应用,提高农业生产效率。",
            }
        )
    )

    assert (
        ContentAdmissionPolicy().evaluate(item).error_code
        == "excluded_topic_minor_technology"
    )


def test_guardian_query_adds_documented_negative_search_terms() -> None:
    query = guardian_query_with_exclusions("kindness")

    assert query.startswith("(kindness) AND NOT (")
    assert "politics" in query
    assert "football" in query
    assert "disaster" in query
    assert "adoption" in query
    assert "bribery" in query
    assert "technology" not in query
