"""lme-price-mcp：金属价格行情 MCP server。

暴露工具：
- get_price(commodity, date)        → 某品种指定日（或之前最近交易日）的价格
- get_trend(commodity, days)        → 区间趋势统计（首末价、涨跌幅、区间高低、波动率、方向）
- list_commodities()                → 支持的品种、单位与数据源

对应题面源 3 的痛点「登录墙 / 接口频控」，实现里体现为：
1. **降级链**：库内新鲜数据 → 免费公开源（Stooq 日线 CSV）→ 陈旧缓存 → 离线合成序列；
2. **限速与退避**：所有请求走 servers/common/http.py，同域名串行 + 最小间隔 + Retry-After；
3. **代理指标诚实标注**：若用代理品种（COMEX 铜期货代理 LME 铜），
   返回体里 is_proxy=true 并给出说明，绝不冒充实盘价。
"""

from __future__ import annotations

import csv
import io
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import pstdev
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common import offline                                # noqa: E402
from servers.common.config import SETTINGS                        # noqa: E402
from servers.common.http import fetch_text_cached                 # noqa: E402
from servers.common.logging_utils import get_logger               # noqa: E402
from servers.common.mcp_protocol import McpServer, prop, run_server, schema  # noqa: E402
from servers.common.models import Envelope, fail, ok              # noqa: E402
from servers.common.store import Store                            # noqa: E402

log = get_logger("lme_price_mcp")
server = McpServer(
    name="lme-price-mcp",
    instructions=(
        "金属价格行情。commodity 支持名称或代码（copper/LME铜、lithium、iron_ore…）。"
        "provenance.degraded=true 表示数据来自缓存或离线合成序列；"
        "is_proxy=true 表示用代理品种近似，必须在最终简报中标注。"
    ),
)
_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


# ------------------------------------------------------------------ 品种表
_COMMODITIES: dict[str, dict[str, Any]] = {
    "copper": {"name": "Copper (LME)", "unit": "USD/t", "currency": "USD",
               "stooq": "hg.f", "is_proxy": True,
               "proxy_note": "以 COMEX 铜期货（hg.f）代理 LME 铜现货：走势高度相关，"
                             "但存在价差、合约换月与计价差异。"},
    "zinc": {"name": "Zinc (LME)", "unit": "USD/t", "currency": "USD",
             "stooq": None, "is_proxy": False, "proxy_note": ""},
    "nickel": {"name": "Nickel (LME)", "unit": "USD/t", "currency": "USD",
               "stooq": None, "is_proxy": False, "proxy_note": ""},
    "aluminium": {"name": "Aluminium (LME)", "unit": "USD/t", "currency": "USD",
                  "stooq": None, "is_proxy": False, "proxy_note": ""},
    "gold": {"name": "Gold (spot)", "unit": "USD/oz", "currency": "USD",
             "stooq": "xauusd", "is_proxy": False, "proxy_note": ""},
    "lithium_carbonate": {"name": "Lithium carbonate (spot, CIF Asia)", "unit": "USD/t",
                          "currency": "USD", "stooq": None, "is_proxy": True,
                          "proxy_note": "锂盐现货无免费公开日线源，使用合成代理序列，"
                                        "仅供趋势参考，不可作为报价依据。"},
    "spodumene_sc6": {"name": "Spodumene concentrate 6% (SC6)", "unit": "USD/t",
                      "currency": "USD", "stooq": None, "is_proxy": True,
                      "proxy_note": "SC6 现货报价无免费公开源，使用合成代理序列。"},
    "iron_ore_62fe": {"name": "Iron ore 62% Fe (CFR China)", "unit": "USD/t",
                      "currency": "USD", "stooq": None, "is_proxy": True,
                      "proxy_note": "铁矿石指数（上海钢联/Mysteel）需授权，"
                                    "当前使用合成代理序列。"},
}

_ALIASES: dict[str, str] = {
    "cu": "copper", "lme铜": "copper", "铜": "copper", "lme copper": "copper",
    "zn": "zinc", "锌": "zinc", "lme锌": "zinc",
    "ni": "nickel", "镍": "nickel", "lme镍": "nickel",
    "al": "aluminium", "铝": "aluminium", "lme铝": "aluminium",
    "au": "gold", "黄金": "gold", "金": "gold",
    "li": "lithium_carbonate", "锂": "lithium_carbonate", "碳酸锂": "lithium_carbonate",
    "lithium": "lithium_carbonate",
    "锂辉石": "spodumene_sc6", "sc6": "spodumene_sc6", "spodumene": "spodumene_sc6",
    "fe": "iron_ore_62fe", "铁矿石": "iron_ore_62fe", "io": "iron_ore_62fe",
    "iron_ore": "iron_ore_62fe", "iron ore": "iron_ore_62fe",
}


def resolve_commodity(value: str) -> tuple[str, dict[str, Any]] | None:
    key = (value or "").strip().lower()
    if not key:
        return None
    key = _ALIASES.get(key, key)
    if key in _COMMODITIES:
        return key, _COMMODITIES[key]
    for canonical in _COMMODITIES:
        if canonical.startswith(key) or key in canonical:
            return canonical, _COMMODITIES[canonical]
    return None


# ------------------------------------------------------------------ 取数
def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fetch_stooq(symbol: str) -> list[dict[str, Any]]:
    """Stooq 日线 CSV（免费、无需登录）。"""
    url = SETTINGS.price.stooq_symbols_url.format(symbol=symbol)
    outcome = fetch_text_cached(url, ttl_s=SETTINGS.cache.price_ttl_s, store=store())
    if not outcome.ok:
        raise RuntimeError(outcome.error or "stooq unavailable")
    points: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(outcome.text)):
        raw_date = (row.get("Date") or "").strip()
        raw_close = (row.get("Close") or "").strip()
        if not raw_date or not raw_close or raw_close.lower() in ("n/a", "null", "-"):
            continue
        try:
            close = float(raw_close)
            trade_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
        except ValueError:
            continue
        points.append({
            "trade_date": trade_date, "price": close,
            "open": _to_float(row.get("Open")), "high": _to_float(row.get("High")),
            "low": _to_float(row.get("Low")), "close": close,
            "volume": _to_float(row.get("Volume")),
        })
    if not points:
        raise RuntimeError("stooq 未返回可用行")
    return points


def _offline_points(canonical: str) -> list[dict[str, Any]]:
    payload = offline.load_price_fixture()
    series = payload.get("series") or {}
    points = series.get(canonical)
    if not points:
        raise RuntimeError(f"离线夹具中没有 {canonical} 的序列")
    return [{**point, "is_proxy": True} for point in points]


def _normalize(points: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "trade_date": str(point.get("trade_date")),
        "price": float(point["price"]),
        "open": point.get("open"),
        "high": point.get("high"),
        "low": point.get("low"),
        "close": point.get("close", point.get("price")),
        "volume": point.get("volume"),
        "is_proxy": bool(point.get("is_proxy", 0) or meta.get("is_proxy")),
    } for point in points]


def load_series(canonical: str, meta: dict[str, Any], *,
                days: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """降级链取数：库内新鲜 → 免费源 → 陈旧缓存 → 离线合成序列。"""
    info: dict[str, Any] = {"origin": None, "degraded": False, "error": None}

    latest = store().latest_price_date(canonical)
    if latest:
        try:
            age_days = (date.today() - date.fromisoformat(latest)).days
        except ValueError:
            age_days = 999
        if age_days <= 3:
            points = store().get_series(canonical, days)
            if points:
                info.update(origin="store", degraded=False)
                return _normalize(points, meta), info

    symbol = meta.get("stooq")
    if symbol and SETTINGS.network_enabled:
        try:
            points = _fetch_stooq(symbol)
            store().upsert_prices([
                {**point, "commodity": canonical, "unit": meta["unit"],
                 "currency": meta["currency"]} for point in points
            ])
            info.update(origin="live")
            return _normalize(store().get_series(canonical, days), meta), info
        except Exception as exc:                        # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"
    elif not SETTINGS.network_enabled:
        info["error"] = "offline mode"

    cached = store().get_series(canonical, max(days * 3, 30))
    if cached:
        info.update(origin="cache", degraded=True)
        return _normalize(cached, meta)[-days:], info

    try:
        points = _offline_points(canonical)
        # 注意：price_points 的主键是 (commodity, trade_date, source)，
        # 这些离线点没有 source，不能直接落库（会破坏主键约束）。
        # 因此离线路径直接用内存序列，只按天数切片。
        info.update(origin="offline_fixture", degraded=True)
        normalized = _normalize(points, meta)
        return normalized[-days:] if days else normalized, info
    except Exception as exc:                            # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
        return [], info


def _direction(change_pct: float | None) -> str:
    if change_pct is None:
        return "unknown"
    if change_pct > 2.0:
        return "up"
    if change_pct < -2.0:
        return "down"
    return "flat"


def _trend_stats(points: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [point["price"] for point in points]
    if not prices:
        return {}
    first, last = prices[0], prices[-1]
    change_pct = (last - first) / first * 100.0 if first else None
    returns = [(prices[i] - prices[i - 1]) / prices[i - 1]
               for i in range(1, len(prices)) if prices[i - 1]]
    volatility = pstdev(returns) * (252 ** 0.5) * 100.0 if len(returns) > 1 else None
    high = max(points, key=lambda point: point["price"])
    low = min(points, key=lambda point: point["price"])
    return {
        "start_date": points[0]["trade_date"],
        "end_date": points[-1]["trade_date"],
        "start_price": round(first, 4),
        "end_price": round(last, 4),
        "change_pct": None if change_pct is None else round(change_pct, 2),
        "min": {"price": round(low["price"], 4), "date": low["trade_date"]},
        "max": {"price": round(high["price"], 4), "date": high["trade_date"]},
        "observations": len(points),
        "annualised_volatility_pct": None if volatility is None else round(volatility, 2),
        "direction": _direction(change_pct),
    }


def _attach_warnings(env: Envelope, meta: dict[str, Any], info: dict[str, Any],
                     canonical: str) -> Envelope:
    env.sources = [{
        "type": info.get("origin") or "unknown",
        "commodity": canonical,
        "url": (SETTINGS.price.stooq_symbols_url.format(symbol=meta["stooq"])
                if meta.get("stooq") else None),
    }]
    if info.get("degraded"):
        env.degraded = True
        message = (f"未取得一手行情（{info.get('error') or 'source unavailable'}），"
                   f"数据来源为 {info.get('origin')}。")
        if info.get("origin") == "offline_fixture":
            message += "离线合成序列仅供演示，不是真实行情，禁止作为报价依据。"
        env.warn("fallback_source", message)
        env.confidence = min(env.confidence, 0.45)
    if meta.get("is_proxy"):
        env.warn("proxy_instrument", meta.get("proxy_note") or "使用代理品种近似。",
                 severity="info")
        env.confidence = min(env.confidence, 0.75)
    return env


# ------------------------------------------------------------------ 工具
@server.tool(
    "get_price",
    "查询某金属品种的价格。date 可传 YYYY-MM-DD；该日无行情时返回之前最近交易日。"
    "返回价格、单位、币种、数据来源与是否为代理指标。",
    schema(
        properties={
            "commodity": prop("string", "品种名或代码，如 copper / LME铜 / lithium / iron_ore"),
            "date": prop("string", "日期 YYYY-MM-DD，默认最新交易日"),
        },
        required=["commodity"],
    ),
)
def get_price(commodity: str, date: str | None = None) -> str:
    resolved = resolve_commodity(commodity)
    if resolved is None:
        return fail("lme-price-mcp.get_price",
                    f"未知品种：{commodity}；支持：{sorted(_COMMODITIES)}",
                    code="unknown_commodity").to_json()
    canonical, meta = resolved
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return fail("lme-price-mcp.get_price",
                        f"日期格式应为 YYYY-MM-DD，收到 {date!r}",
                        code="invalid_date").to_json()

    points, info = load_series(canonical, meta, days=SETTINGS.price.max_trend_days)
    candidates = [p for p in points if p["trade_date"] <= date] if date else points
    if not candidates:
        return fail("lme-price-mcp.get_price",
                    f"{canonical} 在 {date or '可用区间'} 无行情数据"
                    f"（来源：{info.get('origin')}）", code="no_data").to_json()

    latest = candidates[-1]
    env = ok("lme-price-mcp.get_price", {
        "commodity": canonical,
        "display_name": meta["name"],
        "requested_date": date,
        "date": latest["trade_date"],
        "price": latest["price"],
        "open": latest["open"],
        "high": latest["high"],
        "low": latest["low"],
        "close": latest["close"],
        "volume": latest["volume"],
        "unit": meta["unit"],
        "currency": meta["currency"],
        "is_proxy": bool(meta.get("is_proxy")),
        "proxy_note": meta.get("proxy_note", ""),
        "data_origin": info.get("origin"),
        "note": ("请求日期无行情，已回退到该日之前最近交易日。"
                 if date and latest["trade_date"] != date else None),
    })
    return _attach_warnings(env, meta, info, canonical).to_json()


@server.tool(
    "get_trend",
    "查询某品种近 N 天的价格趋势：首末价、涨跌幅、区间高低点、年化波动率与方向判断。",
    schema(
        properties={
            "commodity": prop("string", "品种名或代码，如 copper / lithium / iron_ore"),
            "days": prop("integer", "回溯天数，默认 30，上限 180", default=30),
        },
        required=["commodity"],
    ),
)
def get_trend(commodity: str, days: int = 30) -> str:
    resolved = resolve_commodity(commodity)
    if resolved is None:
        return fail("lme-price-mcp.get_trend",
                    f"未知品种：{commodity}", code="unknown_commodity").to_json()
    canonical, meta = resolved
    days = min(max(int(days), 2), SETTINGS.price.max_trend_days)

    points, info = load_series(canonical, meta, days=days)
    points = points[-days:]
    if len(points) < 2:
        return fail("lme-price-mcp.get_trend",
                    f"{canonical} 可用行情不足 2 个交易日，无法计算趋势"
                    f"（来源：{info.get('origin')}）", code="insufficient_data").to_json()

    env = ok("lme-price-mcp.get_trend", {
        "commodity": canonical,
        "display_name": meta["name"],
        "unit": meta["unit"],
        "currency": meta["currency"],
        "requested_days": days,
        "is_proxy": bool(meta.get("is_proxy")),
        "proxy_note": meta.get("proxy_note", ""),
        "data_origin": info.get("origin"),
        **_trend_stats(points),
        "series_sample": _sample(points, 10),
    })
    return _attach_warnings(env, meta, info, canonical).to_json()


def _sample(points: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    if len(points) <= size:
        return [{"date": p["trade_date"], "price": p["price"]} for p in points]
    step = max(len(points) // size, 1)
    sampled = points[::step][:size]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return [{"date": p["trade_date"], "price": p["price"]} for p in sampled]


@server.tool(
    "list_commodities",
    "列出支持的品种、计价单位、数据源与是否为代理指标。",
    schema(properties={}),
)
def list_commodities() -> str:
    seen: dict[str, dict[str, Any]] = {}
    names: set[str] = set()
    for canonical, meta in _COMMODITIES.items():
        if meta["name"] in names:
            continue
        names.add(meta["name"])
        seen[canonical] = {
            "commodity": canonical,
            "aliases": sorted(alias for alias, target in _ALIASES.items()
                              if target == canonical),
            "display_name": meta["name"],
            "unit": meta["unit"],
            "is_proxy": bool(meta.get("is_proxy")),
            "proxy_note": meta.get("proxy_note", ""),
            "free_source": "stooq" if meta.get("stooq") else None,
        }
    env = ok("lme-price-mcp.list_commodities", {
        "commodities": list(seen.values()),
        "network_enabled": SETTINGS.network_enabled,
        "note": "LME/SHFE/上海钢联官方接口需登录或授权；未配置授权时使用免费源或合成代理序列，"
                "并在返回体中标注 is_proxy / degraded。",
    })
    if not SETTINGS.network_enabled:
        env.warn("offline_mode", "当前为离线模式，所有价格来自本地合成序列。")
        env.degraded = True
        env.confidence = 0.4
    return env.to_json()


def main() -> int:
    log.info("lme-price-mcp starting (network=%s, source=%s)",
             SETTINGS.network_enabled, SETTINGS.price.primary_source)
    return run_server(server)


if __name__ == "__main__":
    raise SystemExit(main())
