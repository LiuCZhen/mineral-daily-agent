"""联网预检：真实访问每个数据源，报告连通性、耗时与响应结构。

为什么需要它：
离线模式（`MDA_OFFLINE=1`）走的是缓存与合成夹具，`servers/common/http.py` 的真实
网络路径在开发机上无法验证（沙箱无外网）。这个脚本把「能不能拿到真实数据」
拆成逐源可判定的小步骤，避免直接用完整 demo 去撞一堵墙。

它会检查：
1. 新闻 RSS（mining.com / S&P Global）—— 能否取到 XML、解析出多少条目、日期是否可用；
2. 价格源（Stooq CSV）—— 格式是否与解析器一致（列名 Date/Open/High/Low/Close/Volume）；
3. 政府站点 HTML（DISR 等）—— 状态码与可抽取正文长度；
4. NI 43-101 PDF 下载 —— 能否取到二进制、大小是否在限制内。

用法：
    python scripts/check_network.py                 # 全部检查
    python scripts/check_network.py --only feeds    # 只查新闻源
    python scripts/check_network.py --only price
    python scripts/check_network.py --only policy
    python scripts/check_network.py --json

退出码：0 = 至少一个新闻源或价格源可用；1 = 全部不可用（此时应回退离线模式）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 注意：本脚本必须能在联网模式下运行，因此不再强制 MDA_OFFLINE
from servers.common.config import SETTINGS                      # noqa: E402
from servers.common.http import FetchError, fetch_sync          # noqa: E402
from servers.mining_news_mcp.feeds import parse_feed, extract_article  # noqa: E402

PROBE_POLICY_URLS = (
    "https://www.industry.gov.au/publications/critical-minerals-strategy",
    "https://www.industry.gov.au/",
)
PROBE_PDF_URL = (
    # 公开可访问的 NI 43-101 技术报告样例（SEDAR 上的发行人技术报告）
    "https://www.sec.gov/Archives/edgar/data/1164727/000116472723000011/"
    "exhibit991.htm"
)
TIMEOUT_S = 25.0


def _ok(status: str) -> bool:
    return status in ("ok", "partial")


def _feed_failure_hint(status: int, body: str) -> dict[str, Any]:
    """把 HTTP 状态翻译成可执行结论 —— 403 与 404 的处理方式完全不同。"""
    head = body[:200].lower()
    if status in (401, 403):
        hint = "被站点拒绝（反爬/WAF）。可尝试：换 MDA_USER_AGENT 为浏览器 UA、"
        hint += "降低 MDA_HTTP_MIN_INTERVAL、或改用其他 RSS 源（见 MDA_NEWS_FEEDS）。"
        if "cloudflare" in head or "cf-" in head:
            hint += " 响应特征像是 Cloudflare 防护。"
        return {"hint": hint, "blocked": True}
    if status == 404:
        return {"hint": "URL 已失效，请更新 MDA_NEWS_FEEDS 中的该条源。", "blocked": False}
    if status >= 500:
        return {"hint": "对方服务端错误，稍后重试即可（非本地问题）。", "blocked": False}
    if status == 200:
        return {"hint": "返回 200 但内容不是 RSS/Atom，可能是 HTML 落地页"
                        "（需先找到真实 feed 地址）。", "blocked": False}
    return {"hint": f"HTTP {status}，请人工确认该源状态。", "blocked": False}


def check_feeds() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for feed_url in SETTINGS.news.feeds:
        entry: dict[str, Any] = {"feed": feed_url}
        started = time.monotonic()
        try:
            response = fetch_sync(feed_url, timeout_s=TIMEOUT_S, retries=1)
            elapsed = int((time.monotonic() - started) * 1000)
            parsed = parse_feed(response.text, source_hint=feed_url)
            dated = [item for item in parsed if item.published_at]
            entry.update(
                status="ok" if parsed else "empty",
                http_status=response.status,
                duration_ms=elapsed,
                bytes=len(response.text),
                items=len(parsed),
                items_with_date=len(dated),
                latest_published=max((item.published_at or "" for item in parsed),
                                     default=None),
                sample_titles=[item.title[:90] for item in parsed[:3]],
            )
            if not parsed:
                entry.update(**_feed_failure_hint(response.status, response.text))
        except FetchError as exc:
            entry.update(status="failed", duration_ms=int((time.monotonic() - started) * 1000),
                         error=str(exc))
        except Exception as exc:                                # noqa: BLE001
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        results.append(entry)
    usable = [r for r in results if _ok(r.get("status", ""))]
    return {"probes": results, "usable": len(usable), "total": len(results)}


def check_price() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for symbol, commodity in (("hg.f", "copper"), ("xauusd", "gold")):
        url = SETTINGS.price.stooq_symbols_url.format(symbol=symbol)
        entry: dict[str, Any] = {"commodity": commodity, "symbol": symbol, "url": url}
        started = time.monotonic()
        try:
            response = fetch_sync(url, timeout_s=TIMEOUT_S, retries=1)
            elapsed = int((time.monotonic() - started) * 1000)
            text = response.text.strip()
            lines = [line for line in text.splitlines() if line.strip()]
            header = lines[0] if lines else ""
            data_rows = lines[1:] if lines else []

            if not lines:
                # 空响应要单独判定：HTTP 200 + 空 body 通常意味着被代理/网关拦截，
                # 与「返回了 CSV 但列名不同」是两回事，不能给同一个误导性提示。
                entry.update(status="empty_body", http_status=response.status,
                             duration_ms=elapsed, bytes=len(response.text))
                entry["hint"] = ("HTTP 200 但响应体为空：通常是网络被代理/网关拦截，"
                                 "而非 Stooq 的 CSV 格式问题。")
            elif "Date" not in header:
                entry.update(status="unexpected", http_status=response.status,
                             duration_ms=elapsed, header=header[:120],
                             rows=len(data_rows))
                entry["hint"] = ("CSV 列名与解析器期望不一致（需要 "
                                 "Date,Open,High,Low,Close,Volume）；"
                                 "请调整 lme_price_mcp/_fetch_stooq。")
            else:
                entry.update(status="ok", http_status=response.status,
                             duration_ms=elapsed, header=header[:120],
                             rows=len(data_rows),
                             first_row=data_rows[0][:120] if data_rows else None,
                             last_row=data_rows[-1][:120] if data_rows else None)
        except FetchError as exc:
            entry.update(status="failed", duration_ms=int((time.monotonic() - started) * 1000),
                         error=str(exc))
        except Exception as exc:                                # noqa: BLE001
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        results.append(entry)
    usable = [r for r in results if _ok(r.get("status", ""))]
    return {"probes": results, "usable": len(usable), "total": len(results)}


def check_policy() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for url in PROBE_POLICY_URLS:
        entry: dict[str, Any] = {"url": url}
        started = time.monotonic()
        try:
            response = fetch_sync(url, timeout_s=TIMEOUT_S, retries=1)
            elapsed = int((time.monotonic() - started) * 1000)
            article = extract_article(response.text, url)
            entry.update(
                status="ok" if article.content else "empty",
                http_status=response.status,
                duration_ms=elapsed,
                bytes=len(response.text),
                title=(article.title or "")[:100],
                content_chars=len(article.content),
                paragraphs=len([p for p in article.content.split("\n\n") if p.strip()]),
                paywall_detected=article.paywall_detected,
                notes=article.warnings,
            )
        except FetchError as exc:
            entry.update(status="failed", duration_ms=int((time.monotonic() - started) * 1000),
                         error=str(exc))
        except Exception as exc:                                # noqa: BLE001
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        results.append(entry)
    usable = [r for r in results if _ok(r.get("status", ""))]
    return {"probes": results, "usable": len(usable), "total": len(results)}


def check_pdf() -> dict[str, Any]:
    """NI 43-101 下载能力。用公开可访问的技术报告页面验证 HTTP 路径。

    注意：本探测只验证「下载二进制/文档」这条链路；完整储量抽取需要真正的
    NI 43-101 PDF（SEDAR 上的多为分章节文件），因此这里报告的是可达性而非抽取质量。
    """
    entry: dict[str, Any] = {"url": PROBE_PDF_URL}
    started = time.monotonic()
    try:
        import httpx

        timeout = httpx.Timeout(TIMEOUT_S, connect=10.0)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(PROBE_PDF_URL,
                                  headers={"User-Agent": SETTINGS.http.user_agent})
        body = response.content
        entry.update(
            status="ok" if response.status_code == 200 and len(body) > 1000 else "unexpected",
            http_status=response.status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes=len(body),
            content_type=response.headers.get("Content-Type", ""),
        )
    except Exception as exc:                                    # noqa: BLE001
        entry.update(status="failed", duration_ms=int((time.monotonic() - started) * 1000),
                     error=f"{type(exc).__name__}: {exc}")
    return {"probes": [entry], "usable": 1 if _ok(entry.get("status", "")) else 0,
            "total": 1}


CHECKS = {"feeds": check_feeds, "price": check_price, "policy": check_policy,
          "pdf": check_pdf}


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("矿权日报 Agent — 联网预检")
    lines.append(f"离线模式: {'是（请先取消 MDA_OFFLINE）' if not SETTINGS.network_enabled else '否'}")
    lines.append(f"限速设置: 同域名最小间隔 {SETTINGS.http.min_interval_s}s，"
                 f"重试 {SETTINGS.http.max_retries} 次，超时 {SETTINGS.http.timeout_s}s")
    lines.append("=" * 70)

    for name, section in report.items():
        lines.append("")
        lines.append(f"### {name}  ({section['usable']}/{section['total']} 可用)")
        for probe in section["probes"]:
            status = probe.get("status")
            icon = {"ok": "✅", "partial": "⚠️", "empty": "⚠️", "empty_body": "⚠️",
                    "unexpected": "⚠️", "failed": "❌"}.get(status, "?")
            label = probe.get("feed") or probe.get("url") or probe.get("symbol")
            lines.append(f"  {icon} [{status}] {label}")
            if probe.get("duration_ms") is not None:
                lines.append(f"       HTTP {probe.get('http_status')} · "
                             f"{probe['duration_ms']} ms · {probe.get('bytes', 0)} bytes")
            for key in ("items", "items_with_date", "latest_published", "rows",
                        "header", "last_row", "title", "content_chars", "paragraphs",
                        "paywall_detected", "content_type"):
                if probe.get(key) not in (None, ""):
                    lines.append(f"       {key}: {probe[key]}")
            for title in probe.get("sample_titles") or []:
                lines.append(f"       例: {title}")
            if probe.get("error"):
                lines.append(f"       错误: {probe['error']}")
            if probe.get("hint"):
                lines.append(f"       提示: {probe['hint']}")
            if probe.get("notes"):
                lines.append(f"       备注: {probe['notes']}")

    lines.append("")
    lines.append("-" * 70)
    verdicts = []
    if "feeds" in report:
        verdicts.append(f"新闻源 {report['feeds']['usable']}/{report['feeds']['total']} 可用")
    if "price" in report:
        verdicts.append(f"价格源 {report['price']['usable']}/{report['price']['total']} 可用")
    if "policy" in report:
        verdicts.append(f"政策页 {report['policy']['usable']}/{report['policy']['total']} 可达")
    if "pdf" in report:
        verdicts.append(f"文档下载 {report['pdf']['usable']}/{report['pdf']['total']} 可达")
    lines.append("；".join(verdicts))
    if any(section["usable"] for section in report.values()):
        lines.append("结论：可以联网运行。建议先单独验证一个源，再跑完整 demo。")
        lines.append("  python scripts/mcp_call.py mining-news-mcp search "
                     "{\\\"query\\\":\\\"copper\\\",\\\"days\\\":7}")
        lines.append("  python -m agent.cli --topic \"Pilbara 锂矿\"   "
                     "# 不加 --offline")
    else:
        lines.append("结论：全部源不可用，请回退离线模式：")
        lines.append("  $env:MDA_OFFLINE = \"1\"")
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实数据源连通性与结构预检")
    parser.add_argument("--only", choices=sorted(CHECKS), action="append",
                        help="只检查指定项（可重复）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if not SETTINGS.network_enabled:
        print("当前为离线模式（MDA_OFFLINE=1 或 MDA_ALLOW_NETWORK=0）。")
        print("本脚本要求联网，请先取消：Remove-Item Env:\\MDA_OFFLINE")
        return 2

    selected = args.only or list(CHECKS)
    report = {name: CHECKS[name]() for name in selected}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(report))

    return 0 if any(section["usable"] for section in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
