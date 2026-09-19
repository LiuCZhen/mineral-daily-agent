"""新闻源解析：RSS/Atom + HTML 正文抽取。

对应题面源 1 的痛点「全文需爬 + 结构化抽取」：
- RSS 只给标题/摘要，正文要二次抓取；正文抽取必须对各家版式容错；
- 优先用 lxml（若有）提升容错，缺失时退化为标准库 HTMLParser + 正则。

真实失败模式（都会在返回值里体现，而不是静默吞掉）：
- 只拿到摘要、未拿到全文 → content_completeness = "summary_only"
- 页面是付费墙/登录墙 → paywall_detected = true，摘要与可抓部分照常返回
"""

from __future__ import annotations

import html as html_lib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.I | re.S)
_WS_RE = re.compile(r"[ \t\u00a0]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")

PAYWALL_HINTS = re.compile(
    r"subscribe to (?:read|continue)|sign in to (?:read|continue)|"
    r"this article is (?:for|available to) subscribers|register to continue|"
    r"create a free account to continue|已被登录墙|订阅后继续阅读",
    re.I,
)

# 正文容器候选（按可靠性从高到低）。
# XPath 版本是主路径：lxml 自带 XPath，不需要额外依赖；
# CSS 版本仅在 cssselect 也可用时作为补充（只装 lxml 不装 cssselect 是常见组合，
# 此时 cssselect() 会抛 ExpressionError，绝不能让它成为主路径）。
CONTENT_XPATHS = (
    "//article",
    "//*[contains(@class,'article-body')]",
    "//*[contains(@class,'article__body')]",
    "//*[contains(@class,'entry-content')]",
    "//*[contains(@class,'post-content')]",
    "//*[contains(@class,'story-body')]",
    "//*[@itemprop='articleBody']",
    "//*[contains(@class,'article-content')]",
    "//main",
)

CONTENT_SELECTORS = (
    "article",
    "div.article-body",
    "div.article__body",
    "div.entry-content",
    "div.post-content",
    "div.story-body",
    "div[itemprop='articleBody']",
    "section.article-content",
    "main",
)

_NOISE_TAGS = ("script", "style", "nav", "aside", "footer", "header", "form", "svg",
               "noscript")


def _meta_nodes(document: Any) -> list:
    """取所有 meta 节点。不依赖 cssselect。"""
    getter = getattr(document, "getroottree", None)
    root = getter().getroot() if callable(getter) else document
    return list(root.iter("meta"))


def _lxml_title(document: Any) -> str:
    """取 <title>。lxml 的 HtmlElement 没有 .title 属性，必须走 XPath。"""
    try:
        nodes = document.xpath("//title")
        if nodes:
            return strip_html(nodes[0].text_content() or "")
    except Exception:                                    # noqa: BLE001
        pass
    return ""


def _lxml_meta_content(document: Any, keys: tuple[str, ...]) -> str | None:
    try:
        for meta in _meta_nodes(document):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop in keys:
                content = meta.get("content")
                if content:
                    return str(content)
    except Exception:                                    # noqa: BLE001
        pass
    return None


def _lxml_published(document: Any) -> str | None:
    raw = _lxml_meta_content(document, (
        "article:published_time", "datepublished", "publishdate", "og:published_time",
        "date", "dc.date", "parsely-pub-date"))
    return parse_date(raw) if raw else None


def _lxml_author(document: Any) -> str:
    raw = _lxml_meta_content(document, ("author", "article:author", "og:author",
                                        "dc.creator"))
    return strip_html(raw or "")


def _body_with_lxml(document: Any, lxml_html: Any) -> str:
    """XPath 优先选正文容器，CSS 补充，最后退化为整页（并保留段落换行）。"""

    def _text_of(node: Any) -> str:
        return strip_html(lxml_html.tostring(node, encoding="unicode"))

    candidates: list[Any] = []
    for expression in CONTENT_XPATHS:
        try:
            candidates.extend(document.xpath(expression))
        except Exception:                                # noqa: BLE001
            continue
    if candidates:
        best = max(candidates, key=lambda node: len(node.text_content() or ""))
        text = _text_of(best)
        if len(text) > 200:
            return text

    try:                                                 # cssselect 可用时再试一次
        for selector in CONTENT_SELECTORS:
            nodes = document.cssselect(selector)
            if nodes:
                best = max(nodes, key=lambda node: len(node.text_content() or ""))
                text = _text_of(best)
                if len(text) > 200:
                    return text
    except Exception:                                    # noqa: BLE001
        pass

    for tag in _NOISE_TAGS:                              # 兜底：清噪后取整页
        for node in document.iter(tag):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
    root = document.body if getattr(document, "body", None) is not None else document
    return _text_of(root)


@dataclass
class ParsedArticle:
    url: str
    title: str = ""
    summary: str = ""
    content: str = ""
    author: str = ""
    published_at: str | None = None
    source: str = ""
    lang: str = "en"
    paywall_detected: bool = False
    content_completeness: str = "unknown"      # full | summary_only | empty
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "author": self.author,
            "published_at": self.published_at,
            "source": self.source,
            "lang": self.lang,
            "paywall_detected": self.paywall_detected,
            "content_completeness": self.content_completeness,
            "warnings": self.warnings,
        }


# ------------------------------------------------------------------ 工具
def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = _SCRIPT_RE.sub(" ", raw)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    # 块级标签与标题都必须断行：漏掉 h1..h6 会让「标题 + 首段」黏成一行，
    # 下游按空行切段时就会把整篇当成一个段落（正文摘要因此显示不全）。
    text = re.sub(r"</(p|div|li|h[1-6]|tr|section|article|blockquote)>", "\n", text,
                  flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def parse_date(value: str | None) -> str | None:
    """统一成 YYYY-MM-DDTHH:MM:SS（UTC，无时区信息）。"""
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %B %Y", "%B %d, %Y"):
        try:
            parsed = datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ RSS
def parse_feed(xml_text: str, *, source_hint: str = "") -> list[ParsedArticle]:
    """解析 RSS 2.0 / Atom。字段缺失不报错，尽量给出可用条目。"""
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        # 有些源返回 HTML 错误页；尝试从错误页里取 <title>
        return []

    items: list[ParsedArticle] = []

    if root.tag.endswith("feed"):                       # Atom
        source = _text(root.find(f"{_ATOM_NS}title")) or source_hint
        for entry in root.findall(f"{_ATOM_NS}entry"):
            link = ""
            for link_el in entry.findall(f"{_ATOM_NS}link"):
                rel = link_el.get("rel", "alternate")
                if rel == "alternate" and link_el.get("href"):
                    link = link_el.get("href", "")
                    break
            if not link:
                link = _text(entry.find(f"{_ATOM_NS}id")) or ""
            summary = _text(entry.find(f"{_ATOM_NS}summary")) or ""
            content = _text(entry.find(f"{_ATOM_NS}content")) or ""
            items.append(ParsedArticle(
                url=link.strip(),
                title=strip_html(_text(entry.find(f"{_ATOM_NS}title")) or ""),
                summary=strip_html(summary)[:1200],
                content=strip_html(content),
                author=strip_html(_text(entry.find(f"{_ATOM_NS}author/{_ATOM_NS}name")) or ""),
                published_at=parse_date(
                    _text(entry.find(f"{_ATOM_NS}updated"))
                    or _text(entry.find(f"{_ATOM_NS}published"))),
                source=source or source_hint,
            ))
        return [item for item in items if item.url]

    channel = root.find("channel") or root
    source = strip_html(_text(channel.find("title")) or "") or source_hint
    for item in channel.findall("item"):
        link = (_text(item.find("link")) or _text(item.find("guid")) or "").strip()
        summary = _text(item.find("description")) or ""
        content = _text(item.find(f"{_CONTENT_NS}encoded")) or ""
        items.append(ParsedArticle(
            url=link,
            title=strip_html(_text(item.find("title")) or ""),
            summary=strip_html(summary)[:1200],
            content=strip_html(content),
            author=strip_html(_text(item.find("author")) or _text(item.find(f"{_DC_NS}creator")) or ""),
            published_at=parse_date(_text(item.find("pubDate"))
                                    or _text(item.find(f"{_DC_NS}date"))),
            source=source or source_hint,
        ))
    return [item for item in items if item.url]


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


# ------------------------------------------------------------ HTML 正文
class _TextCollector(HTMLParser):
    """标准库兜底：收集 body 内可见文本，并跳过 nav/aside/footer。"""

    SKIP_TAGS = {"script", "style", "noscript", "nav", "aside", "footer", "form",
                 "header", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._in_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "body":
            self._in_body = True
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth or not self._in_body:
            return
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = _WS_RE.sub(" ", joined)
        lines = [line.strip() for line in joined.splitlines()]
        return _MULTI_NL_RE.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def extract_article(html_text: str, url: str, *, fallback: ParsedArticle | None = None,
                    max_chars: int = 12000) -> ParsedArticle:
    """HTML → 结构化文章。优先用 lxml 选正文容器，其次标准库兜底。"""
    article = ParsedArticle(url=url)
    if fallback:
        article.title = fallback.title
        article.summary = fallback.summary
        article.source = fallback.source
        article.published_at = fallback.published_at
        article.author = fallback.author

    if not html_text or not html_text.strip():
        article.content = article.summary
        article.content_completeness = "summary_only" if article.summary else "empty"
        article.warnings.append("empty_response_body")
        return article

    article.paywall_detected = bool(PAYWALL_HINTS.search(html_text))

    body = ""
    title = ""
    published = None
    author = ""

    document = None
    try:                                                # 首选 lxml
        import lxml.html as lxml_html                     # type: ignore

        document = lxml_html.fromstring(html_text)
        body = _body_with_lxml(document, lxml_html)
    except Exception:                                    # noqa: BLE001 - 容错解析
        body = ""

    if document is not None:
        # 以下元数据提取各自独立容错：任何一项失败都不应作废已抽到的正文。
        # 典型坑：lxml 的 HtmlElement 没有 .title 属性，直接访问会抛 AttributeError，
        # 若与正文共用一个 try 块，就会把好好的正文一起丢掉。
        title = _lxml_title(document)
        published = _lxml_published(document)
        author = _lxml_author(document)

    if not body:
        collector = _TextCollector()
        try:
            collector.feed(html_text)
        except Exception:                                # noqa: BLE001
            pass
        body = collector.text()
        title = title or strip_html(collector.title)
        article.warnings.append("content_extracted_with_stdlib_fallback")

    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
        title = strip_html(match.group(1)) if match else ""

    if not published:
        match = re.search(
            r'"datePublished"\s*:\s*"([^"]+)"|property="article:published_time"\s+'
            r'content="([^"]+)"', html_text)
        if match:
            published = parse_date(match.group(1) or match.group(2))

    article.title = article.title or title
    article.author = article.author or author
    article.published_at = article.published_at or published

    # 正文里通常带着页面 <h1>，与 title 重复。抽取层就剥掉，别把去重责任推给渲染层
    # （渲染层只负责排版，无法可靠判断哪一行是标题）。
    body = _strip_leading_title(body, article.title)

    if body and (not article.summary or len(body) > len(article.summary)):
        article.content = body[:max_chars]
        article.content_completeness = "full" if len(body) > len(article.summary) + 200 \
            else "summary_only"
    else:
        article.content = article.summary
        article.content_completeness = "summary_only" if article.summary else "empty"

    if article.paywall_detected:
        article.warnings.append("paywall_or_login_wall_detected")
        if article.content_completeness == "full":
            article.content_completeness = "partial_paywalled"

    return article


def _strip_leading_title(body: str, title: str) -> str:
    """去掉正文开头与标题重复的那一行（宽松匹配，容忍站点后缀）。"""
    if not body or not title:
        return body
    paragraphs = body.split("\n\n")
    first = paragraphs[0].strip()
    needle = title.strip()
    if not needle:
        return body
    head = first[:len(needle)].strip()
    if head == needle or (len(head) > 12 and needle.startswith(head)):
        return "\n\n".join(paragraphs[1:]).strip()
    return body
