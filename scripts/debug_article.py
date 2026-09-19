"""诊断：单篇文章的正文抽取结果（渲染层只显示标题时用它定位）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common import offline                        # noqa: E402
from servers.mining_news_mcp import feeds                 # noqa: E402

html = offline.ensure_lithium_article_fixture().read_text(encoding="utf-8")
article = feeds.extract_article(
    html, "https://example.com/synthetic/pilbara-shipments",
    fallback=feeds.ParsedArticle(url="x", title="Pilbara lithium shipments recover as "
                                              "spodumene prices stabilise"))
print("title       :", article.title)
print("completeness:", article.content_completeness)
print("warnings    :", article.warnings)
print("content len :", len(article.content))
print("--- content 前若干段落 ---")
for index, para in enumerate(article.content.split("\n\n")[:8]):
    print(f"  [{index}] ({len(para)}) {para[:160]}")
