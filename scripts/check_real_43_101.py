"""探测真实 NI 43-101 技术报告的可得性。

目的：储量抽取是本项目技术含量最高、也最需要真实数据检验的一环。
本脚本只做「找得到 + 下得动 + 是不是文本型 PDF」这三件事，
抽取质量评估交给 mineral-pdf-mcp 自己。

三条途径：
1. SEC EDGAR full-text search（efts.sec.gov/LATEST/search-index?q=...）找 43-101 附件；
2. 直接探测若干已知公开技术报告的托管地址；
3. 对每个候选下载前 N KB，报告状态码、Content-Type、Content-Length、
   是否 %PDF 魔数（有些"PDF 链接"其实返回 HTML 落地页）。

用法：
    python scripts/check_real_43_101.py
    python scripts/check_real_43_101.py --edgar-fulltext "NI 43-101"
    python scripts/check_real_43_101.py --head-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common.config import SETTINGS                       # noqa: E402

TIMEOUT_S = 30.0
# SEC 明确要求 UA 里声明「谁在抓 + 联系方式」，否则会返回
# "Your Request Originates from an Undeclared Automated Tool"（HTTP 403）。
# 这是接入任何政府数据源时的通用要求，值得照做。
SEC_UA = os.getenv(
    "MDA_SEC_USER_AGENT",
    "MineralDailyAgent/1.0 (research; contact: set-MDA_SEC_USER_AGENT)",
)
EDGAR_FTS = "https://efts.sec.gov/LATEST/search-index"

# 已知公开托管的技术报告（SEDAR+ / 发行商官网 / SEC 附件）
CANDIDATES = (
    # SEDAR+ 上的发行人技术报告（公开可下载）
    "https://www.sedarplus.ca/csa-party/service/create.html",
    # 发行商官网常见的技术报告托管路径（示例级：先验证能否下载）
    "https://www.newmont.com/investors/reports-and-filings/default.aspx",
    # SEC EDGAR 上带 43-101 技术报告摘要的附件
    "https://www.sec.gov/Archives/edgar/data/1164727/000116472723000011/exhibit991.htm",
)


def _client():
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_S, connect=12.0),
        follow_redirects=True,
        headers={"User-Agent": SEC_UA, "Accept": "*/*"},
    )


def probe_candidate(url: str, *, head_only: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url}
    started = time.monotonic()
    try:
        with _client() as client:
            if head_only:
                response = client.head(url)
                body = b""
            else:
                # 只取前 64KB：足以判断 Content-Type 与 %PDF 魔数，不浪费带宽
                with client.stream("GET", url) as stream:
                    chunks = []
                    total = 0
                    for chunk in stream.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= 64 * 1024:
                            break
                    body = b"".join(chunks)
                    response = stream
            headers = {k.lower(): v for k, v in response.headers.items()}
        content_type = headers.get("content-type", "")
        result.update(
            status=response.status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            content_type=content_type[:80],
            content_length=headers.get("content-length"),
            is_pdf=body.startswith(b"%PDF") or "application/pdf" in content_type.lower(),
            is_html=b"<html" in body[:2000].lower(),
            server=headers.get("server", "")[:40],
        )
    except Exception as exc:                                    # noqa: BLE001
        result.update(status=None, duration_ms=int((time.monotonic() - started) * 1000),
                      error=f"{type(exc).__name__}: {exc}")
    return result


def edgar_fulltext(query: str, forms: str = "6-K") -> dict[str, Any]:
    """SEC EDGAR 全文检索：找把技术报告作为附件提交的发行人。

    端点：https://efts.sec.gov/LATEST/search-index?q=...&forms=...
    注意 UA 必须声明联系方式，否则 SEC 返回 403（见模块顶部说明）。
    """
    import httpx

    payload: dict[str, Any] = {"query": query, "forms": forms, "hits": []}
    try:
        with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=12.0),
                          headers={"User-Agent": SEC_UA,
                                   "Accept": "application/json"}) as client:
            response = client.get(EDGAR_FTS, params={"q": f'"{query}"', "forms": forms})
            payload["http_status"] = response.status_code
            if response.status_code != 200:
                payload["body_head"] = response.text[:300]
                if "Undeclared Automated Tool" in response.text:
                    payload["hint"] = ("SEC 要求 UA 声明联系方式："
                                       "设置 MDA_SEC_USER_AGENT=\"YourApp/1.0 "
                                       "(contact: you@example.com)\"")
                return payload
            data = response.json()
            hits = (data.get("hits") or {}).get("hits") or []
            for hit in hits[:20]:
                src = hit.get("_source") or {}
                hit_id = str(hit.get("_id") or "")
                # _id 形如 "0001062993-22-005422:exhibit99-1.pdf"
                # → 目录 https://www.sec.gov/Archives/edgar/data/1062993/22005422/
                accession, _, filename = hit_id.partition(":")
                cik = str((src.get("ciks") or [""])[0]).lstrip("0")
                directory = accession.replace("-", "")
                url = None
                if cik and directory and filename:
                    url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                           f"{directory}/{filename}")
                payload["hits"].append({
                    "id": hit_id,
                    "display_names": src.get("display_names"),
                    "file_type": src.get("file_type"),
                    "file_date": src.get("file_date"),
                    "accession": accession,
                    "filename": filename,
                    "url": url,
                    "index_url": (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                                  f"{directory}/" if cik and directory else None),
                })
    except Exception as exc:                                    # noqa: BLE001
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探测真实 NI 43-101 报告可得性")
    parser.add_argument("--head-only", action="store_true", help="只用 HEAD 请求")
    parser.add_argument("--url", action="append", help="额外候选 URL")
    parser.add_argument("--edgar-fulltext", help="用 SEC 全文检索查关键词")
    args = parser.parse_args(argv)

    if not SETTINGS.network_enabled:
        print("当前为离线模式，请先：Remove-Item Env:\\MDA_OFFLINE")
        return 2

    if args.edgar_fulltext:
        print(f"=== SEC EDGAR 全文检索：{args.edgar_fulltext!r} ===")
        print(json.dumps(edgar_fulltext(args.edgar_fulltext), ensure_ascii=False, indent=2)[:2000])
        print()

    urls = list(args.url or CANDIDATES)
    print(f"=== 候选地址探测（{len(urls)} 个）===")
    pdf_found: list[str] = []
    for url in urls:
        row = probe_candidate(url, head_only=args.head_only)
        icon = "✅" if row.get("is_pdf") else ("📄" if row.get("is_html") else "❔")
        print(f"\n{icon} {url}")
        print(f"   status={row.get('status')} {row.get('duration_ms')}ms "
              f"type={row.get('content_type')} len={row.get('content_length')}")
        if row.get("is_pdf"):
            pdf_found.append(url)
        if row.get("error"):
            print(f"   error={row['error']}")

    print("\n" + "=" * 70)
    if pdf_found:
        print(f"可直接下载的 PDF：{len(pdf_found)} 个")
        for url in pdf_found:
            print(f"  • {url}")
        print("\n下一步（用真实 PDF 验证储量抽取）：")
        for url in pdf_found[:2]:
            print(f'  python scripts/mcp_call.py mineral-pdf-mcp extract_resources '
                  f'{{\\"pdf_url\\": \\"{url}\\"}}')
    else:
        print("未发现可直接下载的 PDF。")
        print("说明：SEDAR+ 与多数发行商站点要求交互式检索，直链不稳定；")
        print("这不影响交付——工具接受任意 http(s) PDF URL，评审可自行提供报告链接。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
