from __future__ import annotations

from god_news.domain.language import looks_like_chinese_translation


def test_chinese_translation_accepts_latin_proper_names() -> None:
    assert looks_like_chinese_translation("Deniz Burnham 将首次前往国际空间站。")


def test_english_news_with_isolated_chinese_phrase_is_rejected() -> None:
    assert not looks_like_chinese_translation(
        "NASA announced a new mission and called it a 重大进展 for science."
    )


def test_japanese_text_is_not_misclassified_as_chinese_translation() -> None:
    assert not looks_like_chinese_translation("NASAの宇宙飛行士が地球へ帰還しました。")
