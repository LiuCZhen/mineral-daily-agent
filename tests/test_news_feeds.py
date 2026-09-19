"""新闻正文抽取的回归测试。

这两个 bug 都真实发生过，且都属于「装了 lxml 反而更差」的类型：

1. `document.cssselect()` 需要额外的 cssselect 包。只装 lxml 时它抛
   `ExpressionError: No module named 'cssselect'`，被宽泛的 except 吞掉后
   整篇退化成标准库兜底（整页文本挤成一段）；
2. `lxml.html.fromstring()` 返回的 HtmlElement **没有 `.title` 属性**，
   直接访问抛 AttributeError；若与正文抽取共用一个 try 块，会把已经抽好的正文一起丢掉。

第 3 个是渲染层问题：`strip_html` 未把 `</h1>` 当换行，导致「标题+首段」黏成一行，
简报里就会只显示标题。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common import offline                          # noqa: E402
from servers.mining_news_mcp import feeds                   # noqa: E402

ARTICLE_URL = "https://example.com/synthetic/pilbara-shipments"


def _fixture_html() -> str:
    return offline.ensure_lithium_article_fixture().read_text(encoding="utf-8")


def test_strip_html_breaks_on_headings_and_blocks() -> None:
    """标题必须独立成行，否则下游按空行切段时会丢掉正文。"""
    result = feeds.strip_html("<h1>Title</h1><p>Body one</p><p>Body two</p>")
    assert result.splitlines()[0] == "Title"
    assert "Body one" in result
    assert "Title" not in result.splitlines()[1]


def test_article_body_is_split_into_paragraphs() -> None:
    article = feeds.extract_article(_fixture_html(), ARTICLE_URL)
    paragraphs = [p for p in article.content.split("\n\n") if p.strip()]
    assert len(paragraphs) >= 3, f"正文应保留段落结构，实际 {len(paragraphs)} 段"
    assert article.content_completeness == "full"
    assert "spodumene concentrate shipments" in article.content


def test_title_is_extracted_without_crashing_on_lxml() -> None:
    """lxml 的 HtmlElement 没有 .title，必须走 XPath 取 <title>。"""
    article = feeds.extract_article(_fixture_html(), ARTICLE_URL)
    assert article.title.startswith("Pilbara lithium shipments recover")
    # 正文开头的 h1 与标题重复，抽取层应已剥掉（渲染层不再需要猜哪行是标题）
    assert not article.content.startswith(article.title)
    assert "SYNTHETIC SAMPLE ARTICLE" in article.content.split("\n\n")[0]


def test_meta_published_time_is_parsed() -> None:
    article = feeds.extract_article(_fixture_html(), ARTICLE_URL)
    assert article.published_at is not None
    assert article.published_at.startswith("2026-")


def test_extraction_does_not_report_stdlib_fallback_for_normal_html() -> None:
    """正文是标准 <article> 结构时不应触发兜底路径。"""
    article = feeds.extract_article(_fixture_html(), ARTICLE_URL)
    assert "content_extracted_with_stdlib_fallback" not in article.warnings


def test_stdlib_fallback_still_works_when_lxml_unavailable(
        monkeypatch) -> None:
    """lxml 不可用时必须仍能抽到正文（退化为标准库 HTMLParser）。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name.startswith("lxml"):
            raise ImportError("simulated: lxml unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    article = feeds.extract_article(_fixture_html(), ARTICLE_URL)
    assert "spodumene concentrate shipments" in article.content
    assert "content_extracted_with_stdlib_fallback" in article.warnings


def test_paywall_detection_marks_content_partial() -> None:
    html = ("<html><body><article><h1>Locked</h1>"
            "<p>Subscribe to read the rest of this article.</p>"
            "<p>" + "body text " * 60 + "</p></article></body></html>")
    article = feeds.extract_article(html, "https://example.com/locked")
    assert article.paywall_detected is True
    assert "paywall_or_login_wall_detected" in article.warnings


def test_empty_body_degrades_to_summary() -> None:
    article = feeds.extract_article("", ARTICLE_URL,
                                    fallback=feeds.ParsedArticle(
                                        url=ARTICLE_URL, title="T", summary="S"))
    assert article.content == "S"
    assert article.content_completeness == "summary_only"
    assert "empty_response_body" in article.warnings
