from __future__ import annotations


def is_chinese_language(language: str | None) -> bool:
    """Return whether a declared source language denotes Chinese."""

    if language is None:
        return False
    normalized = language.strip().casefold().replace("_", "-")
    return normalized in {"zh", "zh-cn", "zh-hans", "zh-hant", "chinese", "cmn"} or (
        normalized.startswith("zh-")
    )


def normalized_language_tag(language: str | None) -> str | None:
    """Normalize a configured language tag without guessing an absent value."""

    if language is None:
        return None
    normalized = language.strip().casefold().replace("_", "-")
    return normalized or None


def same_language(language_a: str | None, language_b: str | None) -> bool:
    """Return whether two explicit language tags identify the same language family."""

    left = normalized_language_tag(language_a)
    right = normalized_language_tag(language_b)
    if left is None or right is None:
        return False
    if is_chinese_language(left) and is_chinese_language(right):
        return True
    return left.split("-", 1)[0] == right.split("-", 1)[0]


def looks_like_chinese_translation(content: str) -> bool:
    """Check that generated output is predominantly Chinese rather than copied source text.

    Translation fields can legitimately be much shorter than source articles, so this
    accepts two Han characters and Latin-script proper names while still rejecting
    English prose containing an isolated Chinese phrase.
    """

    han_count = sum(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in content
    )
    kana_count = sum(
        "\u3040" <= character <= "\u30ff" or "\u31f0" <= character <= "\u31ff"
        for character in content
    )
    letter_count = sum(character.isalpha() for character in content)
    return han_count >= 2 and kana_count == 0 and han_count * 4 >= max(1, letter_count)


def looks_like_chinese_han_text(content: str) -> bool:
    """Conservatively identify unlabelled Chinese Han text.

    Japanese Kana vetoes the fallback.  A Han majority and a modest minimum
    avoid treating a lone proper noun or a mixed English article as Chinese.
    """

    han_count = sum(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in content
    )
    kana_count = sum(
        "\u3040" <= character <= "\u30ff" or "\u31f0" <= character <= "\u31ff"
        for character in content
    )
    letter_count = sum(character.isalpha() for character in content)
    return han_count >= 8 and kana_count == 0 and han_count * 2 >= max(1, letter_count)


def should_preserve_chinese_source(content: str, source_language: str | None) -> bool:
    """Decide whether a translation adapter must retain original Chinese text.

    Explicit non-Chinese metadata wins over the text heuristic.  This protects
    sources which legitimately contain quoted Chinese while declaring another
    source language.
    """

    return is_chinese_language(source_language) or (
        source_language is None and looks_like_chinese_han_text(content)
    )
