from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from god_news.sources.models import (
    DazhongSourceFields,
    GuardianSourceFields,
    NormalizedSourceItem,
    PikabuSourceFields,
    RedditSourceFields,
)

ExcludedTopic = Literal["politics", "sports"]


@dataclass(frozen=True, slots=True)
class SourceAdmissionDecision:
    accepted: bool
    topic: ExcludedTopic | None = None

    @property
    def error_code(self) -> str | None:
        return None if self.topic is None else f"excluded_topic_{self.topic}"


class SourceAdmissionPolicy(Protocol):
    def evaluate(self, item: NormalizedSourceItem) -> SourceAdmissionDecision: ...


_POLITICS_METADATA = frozenset(
    {
        "politics",
        "political",
        "politique",
        "политика",
        "политическое",
        "政治",
        "时政",
    }
)
_SPORTS_METADATA = frozenset(
    {
        "sport",
        "sports",
        "football",
        "soccer",
        "cricket",
        "tennis",
        "basketball",
        "hockey",
        "athletics",
        "спорт",
        "футбол",
        "хоккей",
        "体育",
        "足球",
        "篮球",
    }
)

_POLITICS_PHRASES = (
    "politics",
    "political party",
    "election",
    "elections",
    "parliament",
    "prime minister",
    "president",
    "senator",
    "congress",
    "government",
    "minister",
    "governor",
    "state premier",
    "victorian premier",
    "governor general",
    "government minister",
    "democracy",
    "diplomatic",
    "foreign policy",
    "military strike",
    "ceasefire",
    "pause fire",
    "asylum",
    "refugee protection",
    "war in ",
    "gaza",
    "farage",
    "политика",
    "выборы",
    "парламент",
    "президент",
    "правительство",
    "война",
    "政治",
    "选举",
    "议会",
    "总统",
    "首相",
    "政党",
    "外交",
    "战争",
    "党中央",
    "中央政法委",
    "省委",
    "政府",
    "政策",
    "政务",
    "反腐",
    "扫黑除恶",
)
_SPORTS_PHRASES = (
    "football",
    "soccer",
    "cricket",
    "tennis",
    "basketball",
    "hockey",
    "premier league",
    "world cup",
    "olympic",
    "championship",
    "tournament",
    "grand prix",
    "gold medal",
    "win gold",
    "wins gold",
    "swimmer",
    "спорт",
    "футбол",
    "хоккей",
    "теннис",
    "матч",
    "чемпионат",
    "олимпиад",
    "体育",
    "足球",
    "篮球",
    "乒乓球",
    "网球",
    "世界杯",
    "奥运",
    "锦标赛",
    "冠军赛",
)

_DAZHONG_POLITICS_BODY_PHRASES = (
    "台独",
    "台军",
    "汉光演习",
    "军事演习",
    "军演",
    "民进党",
    "国民党",
    "解放军",
    "两岸关系",
    "防务部门",
    "执政党",
    "在野党",
)
_DAZHONG_SPORTS_BODY_PHRASES = (
    "足球赛",
    "篮球赛",
    "乒乓球赛",
    "世界杯",
    "奥运会",
    "全运会",
    "联赛",
    "锦标赛",
    "冠军赛",
    "中超",
    "英超",
)

_GUARDIAN_QUERY_EXCLUSIONS = (
    "politics",
    "election",
    "parliament",
    "president",
    "football",
    "soccer",
    "cricket",
    "tennis",
    "basketball",
    "hockey",
    '"premier league"',
    '"world cup"',
    "olympic",
)


def guardian_query_with_exclusions(query: str) -> str:
    """Reduce irrelevant Guardian traffic; admission still fails closed downstream."""

    exclusions = " OR ".join(_GUARDIAN_QUERY_EXCLUSIONS)
    return f"({query.strip()}) AND NOT ({exclusions})"


class ContentAdmissionPolicy:
    """Deterministic pre-ingestion guard for topics excluded by the product."""

    def evaluate(self, item: NormalizedSourceItem) -> SourceAdmissionDecision:
        labels = self._metadata_labels(item)
        if self._matches_metadata(labels, _POLITICS_METADATA):
            return SourceAdmissionDecision(accepted=False, topic="politics")
        if self._matches_metadata(labels, _SPORTS_METADATA):
            return SourceAdmissionDecision(accepted=False, topic="sports")

        # Topic inference deliberately uses editorial surfaces, not the full body:
        # incidental mentions inside an otherwise suitable good-news story should
        # not reject the story.
        editorial_text = self._editorial_text(item)
        if self._contains_phrase(editorial_text, _POLITICS_PHRASES):
            return SourceAdmissionDecision(accepted=False, topic="politics")
        if self._contains_phrase(editorial_text, _SPORTS_PHRASES):
            return SourceAdmissionDecision(accepted=False, topic="sports")
        if isinstance(item.source_fields, DazhongSourceFields):
            # Dazhong's public pages often use a generic channel and omit tags. Scan
            # only strong, unambiguous body markers so mixed political/sports
            # roundups cannot enter the editorial queue.
            body = item.content_text.casefold()
            if self._contains_phrase(body, _DAZHONG_POLITICS_BODY_PHRASES):
                return SourceAdmissionDecision(accepted=False, topic="politics")
            if self._contains_phrase(body, _DAZHONG_SPORTS_BODY_PHRASES):
                return SourceAdmissionDecision(accepted=False, topic="sports")
        return SourceAdmissionDecision(accepted=True)

    @staticmethod
    def _metadata_labels(item: NormalizedSourceItem) -> tuple[str, ...]:
        fields = item.source_fields
        if isinstance(fields, GuardianSourceFields):
            values = [fields.section_id, fields.pillar_name, *fields.tags]
        elif isinstance(fields, DazhongSourceFields):
            values = [fields.channel, *fields.tags]
        elif isinstance(fields, RedditSourceFields):
            values = [fields.subreddit, fields.flair]
        elif isinstance(fields, PikabuSourceFields):
            values = list(fields.tags)
        else:
            values = []
        return tuple(value.casefold() for value in values if value)

    @staticmethod
    def _matches_metadata(labels: tuple[str, ...], blocked: frozenset[str]) -> bool:
        for label in labels:
            tokens = {
                token
                for token in re.split(
                    r"[^0-9a-z\u0430-\u044f\u0451\u4e00-\u9fff]+",
                    label,
                )
                if token
            }
            localized_markers = (marker for marker in blocked if not marker.isascii())
            if tokens & blocked or any(marker in label for marker in localized_markers):
                return True
        return False

    @staticmethod
    def _editorial_text(item: NormalizedSourceItem) -> str:
        fields = item.source_fields
        trail = fields.trail_text if isinstance(fields, GuardianSourceFields) else None
        return " ".join(value for value in (item.title, trail) if value).casefold()

    @staticmethod
    def _contains_phrase(value: str, phrases: tuple[str, ...]) -> bool:
        for phrase in phrases:
            normalized = phrase.casefold()
            if normalized.isascii() and normalized.replace(" ", "").isalpha():
                pattern = rf"(?<![a-z]){re.escape(normalized)}(?![a-z])"
                if re.search(pattern, value):
                    return True
            elif normalized in value:
                return True
        return False
