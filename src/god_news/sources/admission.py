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

ExcludedTopic = Literal[
    "politics",
    "sports",
    "disaster",
    "adoption",
    "corruption",
    "minor_technology",
]


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
    "白宫",
    "五角大楼",
    "导弹",
    "国防部",
    "军方",
    "美军",
    "核政策",
    "核武",
    "拥核",
    "右翼政客",
    "军校",
    "大校",
    "基层治理",
    "基层实践",
    "党建",
    "援疆",
    "позывной",
    "на передовой",
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
    "常规赛",
    "赛季",
    "赛事",
    "进球",
    "球迷",
    "球员",
    "球队",
    "比分",
    "超赛",
)

_DISASTER_PHRASES = (
    "disaster",
    "earthquake",
    "tsunami",
    "hurricane",
    "typhoon",
    "tornado",
    "wildfire",
    "landslide",
    "flash flood",
    "flood warning",
    "storm warning",
    "стихийн",
    "землетряс",
    "цунами",
    "наводнен",
    "тайфун",
    "ураган",
    "台风",
    "暴雨",
    "洪水",
    "山洪",
    "地震",
    "海啸",
    "灾害",
    "内涝",
    "山体滑坡",
    "泥石流",
    "山火",
)

_ADOPTION_PHRASES = (
    "for adoption",
    "adopt a",
    "adopt this",
    "adopted from",
    "looking for a home",
    "looking for a family",
    "forever home",
    "needs a home",
    "animal shelter",
    "pet shelter",
    "в добрые руки",
    "ищет дом",
    "ищет семью",
    "приют для животных",
    "попал в приют",
    "живёт в приюте",
    "领养",
    "送养",
    "收养",
    "寻找新主人",
    "寻找温暖的家",
    "寻找家庭",
    "宠物收容所",
    "收容所",
)

_CORRUPTION_PHRASES = (
    "bribery",
    "accepted bribes",
    "accepting bribes",
    "taking bribes",
    "corruption probe",
    "corruption investigation",
    "graft charges",
    "embezzlement",
    "взятк",
    "коррупц",
    "受贿",
    "行贿",
    "贪污",
    "腐败",
    "违纪违法",
    "涉嫌严重违纪",
)

_TECHNOLOGY_PHRASES = (
    "artificial intelligence",
    "generative ai",
    "large language model",
    "foundation model",
    "robotics",
    "robot",
    "technology",
    "tech company",
    "software platform",
    "digital platform",
    "app launch",
    "product launch",
    "ai",
    "искусственный интеллект",
    "робот",
    "технолог",
    "人工智能",
    "大模型",
    "机器人",
    "科技",
    "技术",
    "智能化",
    "数字化",
    "算法",
    "芯片",
    "开放平台",
    "技术应用",
    "科技成果",
    "农业科技",
    "农业技术",
    "水肥一体化",
    "数字农业",
)

_MAJOR_TECHNOLOGY_PHRASES = (
    "major breakthrough",
    "scientific breakthrough",
    "medical breakthrough",
    "landmark discovery",
    "world first",
    "world-first",
    "first ever",
    "first-ever",
    "впервые в мире",
    "научный прорыв",
    "крупный прорыв",
    "重大突破",
    "突破性成果",
    "重大科技成果",
    "重大科学发现",
    "世界首次",
    "全球首次",
    "全球首个",
    "我国首个",
    "首次发现",
    "新矿物",
    "里程碑成果",
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
    "白宫",
    "五角大楼",
    "导弹库存",
    "美国国会",
    "美国总统",
    "美国国防部",
    "军事冲突",
    "武器弹药",
    "核武",
    "拥核",
    "军校",
    "基层治理",
    "基层实践",
    "党建",
    "援疆",
    "市委",
    "市政府",
    "县委",
    "县政府",
    "党委",
    "党支部",
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
    "常规赛",
    "赛季",
    "赛事",
    "进球",
    "球迷",
    "球员",
    "球队",
    "比分",
    "超赛",
    "wtt",
)

_DAZHONG_DISASTER_BODY_PHRASES = (
    "台风预警",
    "暴雨预警",
    "洪水防御",
    "防汛应急响应",
    "山洪灾害",
    "地震灾害",
    "地质灾害",
    "城市内涝",
)

_DAZHONG_CORRUPTION_BODY_PHRASES = (
    "受贿",
    "行贿",
    "贪污",
    "违纪违法",
    "涉嫌严重违纪",
)

_DAZHONG_TECHNOLOGY_BODY_PHRASES = (
    "人工智能",
    "大模型",
    "机器人",
    "农业科技",
    "农业技术",
    "水肥一体化",
    "数字农业",
    "科技成果",
    "芯片研发",
    "算法平台",
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
    "disaster",
    "earthquake",
    "tsunami",
    "hurricane",
    "typhoon",
    "flood",
    "wildfire",
    "adoption",
    '"animal shelter"',
    "bribery",
    "corruption",
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
        full_text = f"{editorial_text} {item.content_text.casefold()}"
        if self._contains_phrase(full_text, _DISASTER_PHRASES):
            return SourceAdmissionDecision(accepted=False, topic="disaster")
        if self._contains_phrase(full_text, _ADOPTION_PHRASES):
            return SourceAdmissionDecision(accepted=False, topic="adoption")
        if self._contains_phrase(full_text, _CORRUPTION_PHRASES):
            return SourceAdmissionDecision(accepted=False, topic="corruption")
        if self._contains_phrase(editorial_text, _TECHNOLOGY_PHRASES) and not self._contains_phrase(
            editorial_text,
            _MAJOR_TECHNOLOGY_PHRASES,
        ):
            return SourceAdmissionDecision(accepted=False, topic="minor_technology")
        if isinstance(item.source_fields, DazhongSourceFields):
            # Dazhong's public pages often use a generic channel and omit tags. Scan
            # only strong, unambiguous body markers so mixed or vaguely titled
            # excluded-topic roundups cannot enter the editorial queue.
            body = item.content_text.casefold()
            if self._contains_phrase(body, _DAZHONG_POLITICS_BODY_PHRASES):
                return SourceAdmissionDecision(accepted=False, topic="politics")
            if self._contains_phrase(body, _DAZHONG_SPORTS_BODY_PHRASES):
                return SourceAdmissionDecision(accepted=False, topic="sports")
            if self._contains_phrase(body, _DAZHONG_DISASTER_BODY_PHRASES):
                return SourceAdmissionDecision(accepted=False, topic="disaster")
            if self._contains_phrase(body, _DAZHONG_CORRUPTION_BODY_PHRASES):
                return SourceAdmissionDecision(accepted=False, topic="corruption")
            if self._contains_phrase(
                body,
                _DAZHONG_TECHNOLOGY_BODY_PHRASES,
            ) and not self._contains_phrase(body, _MAJOR_TECHNOLOGY_PHRASES):
                return SourceAdmissionDecision(accepted=False, topic="minor_technology")
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
