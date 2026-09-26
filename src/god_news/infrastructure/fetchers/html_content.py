from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from god_news.sources.text import normalize_text

_BLOCK_TAGS = frozenset(
    {
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "p",
        "section",
        "tr",
    }
)
_IGNORED_TAGS = frozenset({"head", "script", "style", "noscript", "svg", "template"})
_CHROME_TAGS = frozenset({"aside", "footer", "header", "nav"})
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)", re.IGNORECASE)
_BARE_URL = re.compile(r"https?://[^\s<>\])}\"']+", re.IGNORECASE)
_HTTP_URL = TypeAdapter(AnyHttpUrl)


def _direct_mp4_url(value: str, base_url: str) -> str | None:
    try:
        candidate = urljoin(base_url, unescape(value).strip())
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or not parts.path.casefold().endswith(".mp4")
    ):
        return None
    try:
        return str(
            _HTTP_URL.validate_python(
                urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            )
        )
    except ValidationError:
        return None


def direct_video_links(values: list[str], base_url: str, *, limit: int = 50) -> list[str]:
    """Keep bounded, deduplicated direct MP4 URLs; embeds are not downloadable media."""

    links: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = _direct_mp4_url(value, base_url)
        if url is not None and url not in seen:
            links.append(url)
            seen.add(url)
            if len(links) >= limit:
                break
    return links


def video_links_from_text(text: str, base_url: str) -> list[str]:
    """Find direct media links in a reader's already extracted article text."""

    values = [match.group(1) for match in _MARKDOWN_LINK.finditer(text)]
    values.extend(match.group(0).rstrip(".,;:!?") for match in _BARE_URL.finditer(text))
    return direct_video_links(values, base_url)


def _json_ld_video_urls(payload: object, depth: int = 0) -> list[str]:
    if depth >= 10:
        return []
    if isinstance(payload, list):
        return [url for entry in payload for url in _json_ld_video_urls(entry, depth + 1)]
    if not isinstance(payload, dict):
        return []
    result: list[str] = []
    kind = payload.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if any(
        isinstance(value, str) and value.rsplit("/", 1)[-1].casefold() == "videoobject"
        for value in kinds
    ):
        content = payload.get("contentUrl")
        if isinstance(content, str):
            result.append(content)
        encoding = payload.get("encoding")
        for item in encoding if isinstance(encoding, list) else [encoding]:
            if isinstance(item, dict) and isinstance(item.get("contentUrl"), str):
                result.append(item["contentUrl"])
    for key in ("@graph", "mainEntity", "video", "hasPart"):
        result.extend(_json_ld_video_urls(payload.get(key), depth + 1))
    return result


@dataclass(frozen=True, slots=True)
class HtmlContent:
    text: str
    video_links: list[str]


class _ContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self._body_parts: list[str] = []
        self._main_parts: list[str] = []
        self._tag_videos: list[tuple[str, bool]] = []
        self._structured_videos: list[str] = []
        self._json_script: list[str] | None = None
        self.has_main = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {key.casefold(): value for key, value in attrs if value is not None}
        inside_main = tag in {"article", "main"} or any(
            ancestor in {"article", "main"} for ancestor in self._stack
        )
        if tag in {"article", "main"}:
            self.has_main = True
        if tag == "meta":
            marker = (attributes.get("property") or attributes.get("name") or "").casefold()
            if marker in {
                "og:video",
                "og:video:url",
                "og:video:secure_url",
                "twitter:player:stream",
            }:
                if content := attributes.get("content"):
                    self._structured_videos.append(content)
        if tag == "script" and attributes.get("type", "").casefold() == "application/ld+json":
            self._json_script = []
        if tag in {"video", "source"} and (tag == "video" or "video" in self._stack):
            for key in ("src", "data-src"):
                if source := attributes.get(key):
                    self._tag_videos.append((source, inside_main))
        if tag == "a" and inside_main and (href := attributes.get("href")):
            self._tag_videos.append((href, True))
        if tag in _BLOCK_TAGS:
            self._body_parts.append("\n")
            if inside_main:
                self._main_parts.append("\n")
        if tag not in {"meta", "link", "img", "source", "br", "hr", "input", "embed"}:
            self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "script" and self._json_script is not None:
            try:
                self._structured_videos.extend(
                    _json_ld_video_urls(json.loads("".join(self._json_script)))
                )
            except (TypeError, ValueError, RecursionError):
                pass
            self._json_script = None
        if tag in _BLOCK_TAGS:
            self._body_parts.append("\n")
            self._main_parts.append("\n")
        if tag in self._stack:
            del self._stack[len(self._stack) - 1 - self._stack[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if self._json_script is not None:
            self._json_script.append(data)
        if any(tag in _IGNORED_TAGS | _CHROME_TAGS for tag in self._stack):
            return
        self._body_parts.append(data)
        if any(tag in {"article", "main"} for tag in self._stack):
            self._main_parts.append(data)

    def finish(self, base_url: str) -> HtmlContent:
        main = normalize_text("".join(self._main_parts))
        body = normalize_text("".join(self._body_parts))
        tag_videos = [url for url, in_main in self._tag_videos if in_main or not self.has_main]
        video_links = direct_video_links(self._structured_videos + tag_videos, base_url)
        return HtmlContent(text=main or body, video_links=video_links)


def extract_html_content(html: str, base_url: str) -> HtmlContent:
    """Extract visible article text and direct video references from bounded HTML."""

    parser = _ContentParser()
    parser.feed(html)
    return parser.finish(base_url)


def choose_article_text(extracted: str | None, visible: str, min_characters: int) -> str:
    """Prefer the article extractor, falling back to visible main content when weak."""

    primary = normalize_text(extracted or "")
    if len(primary) >= min_characters or len(primary) >= len(visible):
        return primary
    return visible
