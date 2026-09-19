"""端到端验证：三个 MCP server 的关键工具调用（离线也必须可跑）。

用法：
    python scripts/verify_tools.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.mcp_registry import build_clients, spec_by_key  # noqa: E402
from servers.common.config import FIXTURE_DIR              # noqa: E402


def call(key: str, tool: str, arguments: dict) -> tuple[bool, dict | None, str]:
    spec = spec_by_key(key)
    client = build_clients([spec])[0]
    client.start()
    try:
        result = client.call_tool(tool, arguments)
        data = result.data if isinstance(result.data, dict) else None
        return result.ok, data, result.text
    finally:
        client.close()


def main() -> int:
    failures = 0

    # 1) 新闻检索
    ok_, data, text = call("mining-news-mcp", "search",
                           {"query": "Pilbara lithium", "days": 30, "limit": 3})
    count = len((data or {}).get("data", {}).get("results", []))
    print(f"[news.search] ok={ok_} results={count} degraded="
          f"{(data or {}).get('provenance', {}).get('degraded')}")
    if not ok_ or count == 0:
        failures += 1
        print(text[:600])

    # 2) 新闻全文（用检索到的第一条，避免硬编码 URL）
    results = (data or {}).get("data", {}).get("results", [])
    if results:
        ok_, data2, text2 = call("mining-news-mcp", "fetch_article",
                                 {"url": results[0]["url"], "max_chars": 1500})
        chars = ((data2 or {}).get("data", {}).get("article", {}) or {}).get("content_chars")
        print(f"[news.fetch_article] ok={ok_} content_chars={chars}")
        if not ok_:
            failures += 1
            print(text2[:600])

    # 3) 储量抽取（本地样例 PDF，避免依赖外网）
    sample = FIXTURE_DIR / "newmont_ni43-101_synthetic.pdf"
    ok_, data3, text3 = call("mineral-pdf-mcp", "extract_resources",
                             {"pdf_url": sample.as_uri()})
    categories = ((data3 or {}).get("data", {}) or {}).get("resources") or {}
    print(f"[pdf.extract_resources] ok={ok_} categories={sorted(categories)}")
    if not ok_ or not categories:
        failures += 1
        print(text3[:800])
    else:
        print("   ", json.dumps(categories, ensure_ascii=False)[:300])

    # 4) 价格查询与趋势（日期用相对时间，避免硬编码过期日期）
    today = date.today().isoformat()
    ok_, data4, text4 = call("lme-price-mcp", "get_price",
                             {"commodity": "copper", "date": today})
    price = ((data4 or {}).get("data", {}) or {}).get("price")
    print(f"[price.get_price] ok={ok_} price={price} origin="
          f"{((data4 or {}).get('data') or {}).get('data_origin')}")
    if not ok_ or price is None:
        failures += 1
        print(text4[:600])

    ok_, data5, text5 = call("lme-price-mcp", "get_trend",
                             {"commodity": "copper", "days": 30})
    change = ((data5 or {}).get("data", {}) or {}).get("change_pct")
    print(f"[price.get_trend] ok={ok_} change_pct={change}")
    if not ok_:
        failures += 1
        print(text5[:600])

    print(f"\nfailures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
