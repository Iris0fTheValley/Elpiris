from __future__ import annotations

from god_news.domain.language import looks_like_chinese_translation


def test_chinese_translation_accepts_latin_proper_names() -> None:
    assert looks_like_chinese_translation("Royal Cothrun 帮助迷路老人安全获救。")


def test_english_news_with_isolated_chinese_phrase_is_rejected() -> None:
    assert not looks_like_chinese_translation(
        "The Guardian published a report and called it a 温暖善举 for the community."
    )


def test_japanese_text_is_not_misclassified_as_chinese_translation() -> None:
    assert not looks_like_chinese_translation("少年が迷子の高齢者を安全な場所へ案内しました。")
