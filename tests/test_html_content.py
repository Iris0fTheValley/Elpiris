from __future__ import annotations

from god_news.infrastructure.fetchers.html_content import (
    choose_article_text,
    extract_html_content,
    video_links_from_text,
)


def test_html_content_prefers_article_and_discovers_direct_video_sources() -> None:
    html = """
    <html><head>
      <meta property="og:video" content="https://media.example/clip.mp4?quality=high">
      <script type="application/ld+json">
        {"@graph":[{"@type":"VideoObject","contentUrl":"/clip.mp4"}]}
      </script>
    </head><body>
      <nav>Many unrelated navigation words</nav>
      <article><h1>Story</h1><p>A person <strong>helped</strong> a neighbour.</p>
        <video><source src="/source.mp4?token=abc&amp;quality=high"></video>
        <a href="/linked.mp4">Watch the source clip</a>
        <iframe src="https://video.example/embed/42"></iframe>
      </article>
      <aside><video src="/advert.mp4"></video></aside>
      <footer>Unrelated links</footer>
    </body></html>
    """
    result = extract_html_content(html, "https://news.example/story")

    assert "A person helped a neighbour." in result.text
    assert "navigation" not in result.text
    assert "Unrelated" not in result.text
    assert result.video_links == [
        "https://media.example/clip.mp4?quality=high",
        "https://news.example/clip.mp4",
        "https://news.example/source.mp4?token=abc&quality=high",
        "https://news.example/linked.mp4",
    ]


def test_html_content_falls_back_to_visible_body_and_rejects_embeds() -> None:
    html = """
    <body><header>Site header</header><div>Useful <b>body</b> copy.</div>
    <video src="javascript:alert(1)"></video>
    <iframe src="https://player.example/watch/42"></iframe>
    <a href="https://media.example/clip.m3u8">Stream</a></body>
    """
    result = extract_html_content(html, "https://news.example/story")

    assert result.text.startswith("Useful body copy.")
    assert result.video_links == []
    assert choose_article_text("short", result.text, 20) == result.text
    assert choose_article_text("A verified article body.", result.text, 20) == (
        "A verified article body."
    )


def test_reader_markdown_discovers_only_direct_mp4_urls() -> None:
    result = video_links_from_text(
        "[watch](https://cdn.example/clip.mp4?token=x) "
        "https://cdn.example/clip.mp4?token=x "
        "[player](https://video.example/embed/42) "
        "https://cdn.example/manifest.m3u8",
        "https://news.example/story",
    )

    assert result == ["https://cdn.example/clip.mp4?token=x"]
