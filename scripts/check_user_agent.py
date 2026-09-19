"""对比不同 User-Agent 对目标源的访问结果，定位反爬策略。

为什么单独做这件事：
`mining.com` 等站点常见 403，而 403 的原因决定了修法完全不同：
- 若是 UA 黑名单 → 换成浏览器 UA 即可；
- 若是 Cloudflare 质询 / IP 级封禁 → 换 UA 无效，需要换源或加代理。

本脚本对同一 URL 依次用默认 UA 与浏览器 UA 请求，并打印状态码、响应长度与
特征串（cloudflare / cf-ray / captcha 等），把「能不能修」变成可判定结论。

用法：
    python scripts/check_user_agent.py
    python scripts/check_user_agent.py --url https://www.mining.com/feed/
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common.config import SETTINGS                     # noqa: E402
from servers.mining_news_mcp.feeds import parse_feed           # noqa: E402

DEFAULT_URLS = list(SETTINGS.news.feeds) + [
    "https://www.northernminer.com/feed/",
    "https://www.mining-technology.com/feed/",
    "https://www.spglobal.com/marketintelligence/en/rss/all",
]

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

FEATURE_TOKENS = ("cloudflare", "cf-ray", "cf-mitigated", "captcha", "just a moment",
                  "access denied", "attention required", "enable javascript")

TIMEOUT_S = 25.0


def probe(url: str, user_agent: str, label: str) -> dict[str, Any]:
    import httpx

    result: dict[str, Any] = {"label": label, "url": url, "user_agent": user_agent[:48]}
    started = time.monotonic()
    try:
        timeout = httpx.Timeout(TIMEOUT_S, connect=10.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml,application/xml,text/xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
        body = response.text or ""
        low = body.lower()
        result.update(
            status=response.status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes=len(body),
            items=len(parse_feed(body, source_hint=url)) if response.status_code == 200 else 0,
            server=response.headers.get("Server", ""),
            features=[token for token in FEATURE_TOKENS if token in low][:4],
        )
    except Exception as exc:                                    # noqa: BLE001
        result.update(status=None, duration_ms=int((time.monotonic() - started) * 1000),
                      error=f"{type(exc).__name__}: {exc}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对比 User-Agent 以判断反爬策略")
    parser.add_argument("--url", action="append", help="要探测的 URL（可重复）")
    args = parser.parse_args(argv)

    if not SETTINGS.network_enabled:
        print("当前为离线模式，请先：Remove-Item Env:\\MDA_OFFLINE")
        return 2

    urls = args.url or DEFAULT_URLS
    verdicts: list[str] = []

    for url in urls:
        print(f"\n=== {url} ===")
        rows = [probe(url, SETTINGS.http.user_agent, "默认 UA(爬虫标识)"),
                probe(url, BROWSER_UA, "浏览器 UA")]
        for row in rows:
            status = row.get("status")
            print(f"  [{row['label']}] status={status} "
                  f"{row.get('duration_ms')}ms {row.get('bytes', 0)}B "
                  f"items={row.get('items', 0)}"
                  + (f" server={row['server']}" if row.get("server") else "")
                  + (f" 特征={row['features']}" if row.get("features") else "")
                  + (f" 错误={row['error']}" if row.get("error") else ""))

        default_row, browser_row = rows
        if default_row.get("items") or browser_row.get("items"):
            best = "默认 UA" if default_row.get("items") else "浏览器 UA"
            verdicts.append(f"{url} → 可用（{best}）")
        elif default_row.get("status") == 403 and browser_row.get("status") == 200:
            verdicts.append(f"{url} → 403 由 UA 触发，浏览器 UA 可通过："
                            f"设置 MDA_USER_AGENT 即可")
        elif default_row.get("status") == 403 and browser_row.get("status") == 403:
            verdict = f"{url} → 两种 UA 均 403，疑似 WAF/Cloudflare 质询或 IP 级限制"
            if any("cloudflare" in f or "cf-" in f
                   for f in (default_row.get("features") or [])):
                verdict += "（响应含 Cloudflare 特征）"
            verdict += "；建议改用其他 RSS 源"
            verdicts.append(verdict)
        elif default_row.get("status") == 200 and browser_row.get("status") == 200:
            verdicts.append(f"{url} → 200 但未解析出条目：可能不是 RSS/Atom 地址")
        else:
            verdicts.append(f"{url} → 探测失败（见上方错误）")

    print("\n" + "=" * 70)
    for line in verdicts:
        print("• " + line)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
