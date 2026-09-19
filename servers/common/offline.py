"""离线夹具：断网/被登录墙拦住时，让服务依然能跑出可验证的结果。

诚实性要求（写进 DATA_NOTES 与简报脚注）：
- 夹具内容全部是**合成的示例数据**，不是真实行情或真实新闻，只在演示/测试时使用；
- 任何一个使用了夹具的返回体都会被打上 provenance.degraded = true 与显式告警，
  Agent 必须把这一条写进简报的「数据可靠性」章节，绝不能当作真实数据汇报。
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from servers.common.config import FIXTURE_DIR, ensure_dirs

OFFLINE_DIR = FIXTURE_DIR / "offline"


def offline_dir() -> Path:
    ensure_dirs()
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    return OFFLINE_DIR


# ------------------------------------------------------------------ 新闻
def ensure_news_fixture() -> Path:
    """生成合成新闻语料（若不存在）。内容覆盖锂/铜/政策三类，便于检索演示。"""
    path = offline_dir() / "mining_news_sample.json"
    if path.exists():
        return path

    today = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []

    def add(days_ago: int, title: str, summary: str, source: str, slug: str,
            commodities: list[str], regions: list[str], body: list[str]) -> None:
        published = (today - timedelta(days=days_ago)).replace(microsecond=0)
        items.append({
            "title": title,
            "url": f"https://example.com/synthetic/{slug}",
            "source": source,
            "published_at": published.strftime("%Y-%m-%dT%H:%M:%S"),
            "summary": summary,
            "content": "\n\n".join(body),
            "commodities": commodities,
            "regions": regions,
            "synthetic": True,
        })

    add(
        0, "Pilbara lithium shipments recover as spodumene prices stabilise",
        "Pilbara Minerals reported higher spodumene concentrate shipments while "
        "spot prices steadied after a two-quarter decline.",
        "mining.com (synthetic sample)", "pilbara-shipments",
        ["lithium"], ["AU"],
        [
            "SYNTHETIC SAMPLE ARTICLE — not a real news report.",
            "The producer said spodumene concentrate shipments rose quarter on quarter "
            "as offtake partners in China restocked, while realised prices remained "
            "broadly flat after two consecutive quarters of decline.",
            "Analysts cautioned that Chinese lithium carbonate inventories remain "
            "elevated, and that any price recovery depends on downstream EV demand "
            "in the second half of the year.",
        ],
    )
    add(
        1, "Australia tightens critical minerals investment screening",
        "The Department of Industry, Science and Resources updated guidance on "
        "foreign investment in critical minerals projects.",
        "DISR (synthetic sample)", "disr-critical-minerals-guidance",
        ["lithium", "rare earths"], ["AU"],
        [
            "SYNTHETIC SAMPLE ARTICLE — not a real government notice.",
            "Updated guidance clarifies that proposed acquisitions of critical "
            "minerals projects may be screened where the target holds a strategic "
            "deposit, and sets out expectations on downstream processing commitments.",
            "The change is described as a procedural clarification rather than a "
            "tightening of the existing threshold, but legal advisers expect longer "
            "review timelines for offshore bidders.",
        ],
    )
    add(
        3, "Copper concentrate treatment charges fall to multi-year lows",
        "Spot treatment and refining charges for copper concentrate extended losses "
        "as smelter capacity outpaced mine supply growth.",
        "mining.com (synthetic sample)", "copper-tcrc-lows",
        ["copper"], ["CL", "CN"],
        [
            "SYNTHETIC SAMPLE ARTICLE — not a real news report.",
            "Spot TC/RCs for copper concentrate fell further, reflecting continued "
            "smelter expansion in China against limited growth in mined concentrate "
            "supply. Producers with high-cost operations face margin pressure.",
            "The development is relevant to copper price direction and to smelter "
            "economics, though it does not by itself imply a change in refined "
            "copper prices.",
        ],
    )
    add(
        5, "China rare earth quota pace draws scrutiny from buyers",
        "Buyers are watching quota issuance cadence from the China Rare Earth Group "
        "for signals on supply availability.",
        "S&P Global (synthetic sample)", "rare-earth-quota-scrutiny",
        ["rare earths"], ["CN"],
        [
            "SYNTHETIC SAMPLE ARTICLE — not a real news report.",
            "Market participants are monitoring the pace of mining and smelting "
            "quota issuance, which has historically influenced short-term prices "
            "for praseodymium-neodymium oxides.",
            "No specific quota volume changes have been confirmed in this sample.",
        ],
    )
    add(
        8, "Nickel market faces surplus as Indonesian supply expands",
        "Higher Indonesian nickel output continues to pressure class-1 nickel prices.",
        "mining.com (synthetic sample)", "nickel-surplus",
        ["nickel"], ["ID"],
        [
            "SYNTHETIC SAMPLE ARTICLE — not a real news report.",
            "Rising Indonesian supply has kept the global nickel market in surplus, "
            "with LME inventories climbing and prices under pressure.",
        ],
    )
    add(
        12, "Pilbara exploration results extend lithium mineralisation at depth",
        "Drilling extended high-grade spodumene mineralisation below the current "
        "resource pit shell.",
        "S&P Global (synthetic sample)", "pilbara-exploration",
        ["lithium"], ["AU"],
        [
            "SYNTHETIC SAMPLE ARTICLE — not a real news report.",
            "Assay results from extensional drilling intersected spodumene-bearing "
            "pegmatite below the existing resource outline, which may support a "
            "future resource upgrade if confirmed by infill drilling.",
        ],
    )

    payload = {
        "generated": True,
        "note": "SYNTHETIC sample news corpus for offline demonstration only. "
                "Not real reporting; do not cite as fact.",
        "items": items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_news_fixture() -> list[dict[str, Any]]:
    path = ensure_news_fixture()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("items") or [])


# ------------------------------------------------------------------ 价格
_PRICE_BASE = {
    # 合成基准价（仅演示）：品种 → (基准价, 单位, 币种, 年化波动率)
    "copper": (9150.0, "USD/t", "USD", 0.22),
    "zinc": (2780.0, "USD/t", "USD", 0.24),
    "nickel": (16200.0, "USD/t", "USD", 0.30),
    "aluminium": (2450.0, "USD/t", "USD", 0.20),
    "lithium_carbonate": (10800.0, "USD/t", "USD", 0.45),
    "spodumene_sc6": (890.0, "USD/t", "USD", 0.48),
    "iron_ore_62fe": (104.0, "USD/t", "USD", 0.28),
    "gold": (2380.0, "USD/oz", "USD", 0.16),
}


def ensure_price_fixture(days: int = 200) -> Path:
    """生成合成日行情序列（确定性伪随机，保证可重复验证）。"""
    path = offline_dir() / "price_history_sample.json"
    if path.exists():
        return path

    rng = random.Random(20250214)
    end = date.today()
    series: dict[str, list[dict[str, Any]]] = {}

    for commodity, (base, unit, currency, vol) in _PRICE_BASE.items():
        points: list[dict[str, Any]] = []
        price = base
        daily_sigma = vol / math.sqrt(252)
        for offset in range(days, -1, -1):
            day = end - timedelta(days=offset)
            if day.weekday() >= 5:            # 周末不交易
                continue
            drift = rng.gauss(0, daily_sigma)
            price = max(price * (1 + drift), base * 0.25)
            points.append({
                "trade_date": day.isoformat(),
                "price": round(price, 4),
                "open": round(price * (1 - rng.uniform(0, 0.006)), 4),
                "high": round(price * (1 + rng.uniform(0.001, 0.012)), 4),
                "low": round(price * (1 - rng.uniform(0.001, 0.012)), 4),
                "close": round(price, 4),
                "volume": float(rng.randint(2000, 90000)),
            })
        series[commodity] = points

    payload = {
        "generated": True,
        "note": "SYNTHETIC price history for offline demonstration only. "
                "Not real market data; do not use for trading or disclosure.",
        "currency": "USD",
        "series": series,
        "units": {c: v[1] for c, v in _PRICE_BASE.items()},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_price_fixture() -> dict[str, Any]:
    return json.loads(ensure_price_fixture().read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 政策
def ensure_policy_fixture() -> Path:
    """政府官网类源：结构不规整 HTML，这里给出一份可复现的离线 HTML 快照。"""
    path = offline_dir() / "policy_page_sample.html"
    if path.exists():
        return path
    html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Critical Minerals Strategy — update (SYNTHETIC SAMPLE)</title></head>
<body>
<nav><a href="/">Home</a> | <a href="/news">News</a></nav>
<article>
  <h1>Critical Minerals Strategy — update</h1>
  <p class="meta">Published 2026-01-15 · Department of Industry, Science and Resources
     (SYNTHETIC SAMPLE)</p>
  <p>SYNTHETIC SAMPLE PAGE — generated for offline testing, not a real government notice.</p>
  <p>The updated strategy maintains the existing list of critical minerals and adds
     guidance on downstream processing investment. Projects that include domestic
     refining capacity may receive streamlined assessment.</p>
  <p>For lithium, the strategy notes that export volumes remain concentrated in
     spodumene concentrate, and that conversion capacity outside China remains limited.</p>
  <p>Industry consultation on the revised guidance closes at the end of the quarter.</p>
</article>
<footer>© SYNTHETIC SAMPLE</footer>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    return path


def ensure_lithium_article_fixture() -> Path:
    """锂矿新闻页的离线快照（与 news 夹具里的 pilbara 条目对应）。"""
    path = offline_dir() / "article_pilbara_shipments.html"
    if path.exists():
        return path
    html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Pilbara lithium shipments recover as spodumene prices stabilise (SYNTHETIC)</title>
<meta property="article:published_time" content="2026-03-02T08:15:00Z">
<meta name="author" content="SYNTHETIC SAMPLE DESK">
</head>
<body>
<nav><a href="/">Home</a> | <a href="/lithium">Lithium</a></nav>
<article>
  <h1>Pilbara lithium shipments recover as spodumene prices stabilise</h1>
  <p>SYNTHETIC SAMPLE ARTICLE — generated for offline testing, not a real news report.</p>
  <p>The producer reported higher spodumene concentrate shipments quarter on quarter as
     offtake partners in China restocked, while realised prices remained broadly flat
     after two consecutive quarters of decline.</p>
  <p>Analysts cautioned that Chinese lithium carbonate inventories remain elevated, and
     that any price recovery depends on downstream battery demand in the second half of
     the year.</p>
  <p>On the policy side, updated guidance on foreign investment screening may lengthen
     review timelines for offshore bidders in critical minerals projects.</p>
</article>
<footer>© SYNTHETIC SAMPLE</footer>
</body></html>
"""
    path.write_text(html, encoding="utf-8")
    return path


def fixture_for_url(url: str) -> str | None:
    """把真实 URL 映射到本地合成快照，使离线模式下的全文抓取路径依然可被验证。

    只映射已知的合成样例域名，绝不把真实站点映射到假内容 ——
    真实 URL 在离线模式下会走「摘要降级 + 明确告警」的路径。
    """
    lowered = (url or "").lower()
    if "example.com/synthetic/pilbara" in lowered:
        return ensure_lithium_article_fixture().read_text(encoding="utf-8")
    if "example.com/synthetic/disr" in lowered:
        return ensure_policy_fixture().read_text(encoding="utf-8")
    return None
