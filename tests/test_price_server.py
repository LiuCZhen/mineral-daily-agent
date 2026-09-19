"""价格 server 的单元测试：别名解析、趋势统计、降级与代理标注。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.lme_price_mcp import server as price_server      # noqa: E402


def test_alias_resolution() -> None:
    assert price_server.resolve_commodity("copper")[0] == "copper"
    assert price_server.resolve_commodity("LME铜")[0] == "copper"
    assert price_server.resolve_commodity("铜")[0] == "copper"
    assert price_server.resolve_commodity("锂")[0] == "lithium_carbonate"
    assert price_server.resolve_commodity("铁矿石")[0] == "iron_ore_62fe"
    assert price_server.resolve_commodity("unobtainium") is None


def test_trend_stats_math() -> None:
    points = [
        {"trade_date": "2026-01-01", "price": 100.0},
        {"trade_date": "2026-01-02", "price": 110.0},
        {"trade_date": "2026-01-05", "price": 90.0},
    ]
    stats = price_server._trend_stats(points)                  # noqa: SLF001
    assert stats["start_price"] == 100.0
    assert stats["end_price"] == 90.0
    assert stats["change_pct"] == -10.0
    assert stats["max"]["price"] == 110.0
    assert stats["min"]["price"] == 90.0
    assert stats["direction"] == "down"
    assert stats["observations"] == 3


def test_direction_thresholds() -> None:
    assert price_server._direction(0.5) == "flat"              # noqa: SLF001
    assert price_server._direction(5.0) == "up"                 # noqa: SLF001
    assert price_server._direction(-5.0) == "down"              # noqa: SLF001
    assert price_server._direction(None) == "unknown"           # noqa: SLF001


def test_get_price_payload_marks_proxy_and_degradation() -> None:
    payload = json.loads(price_server.get_price("copper"))
    assert payload["data"]["price"] > 0
    assert payload["data"]["is_proxy"] is True                  # COMEX 代理 LME
    assert payload["provenance"]["degraded"] is True             # 离线合成序列
    codes = {warning["code"] for warning in payload["warnings"]}
    assert "proxy_instrument" in codes
    assert "fallback_source" in codes


def test_get_price_falls_back_to_previous_trading_day() -> None:
    """请求未来日期（必然没有行情）时必须回退到最近交易日，并显式说明。"""
    payload = json.loads(price_server.get_price("copper", "2099-01-01"))
    assert payload["data"]["date"] < "2099-01-01"
    assert payload["data"]["note"]
    assert payload["data"]["requested_date"] == "2099-01-01"


def test_get_price_rejects_bad_date_and_commodity() -> None:
    bad_date = json.loads(price_server.get_price("copper", "01/02/2026"))
    assert bad_date["error"] and bad_date["provenance"]["confidence"] == 0.0
    bad_name = json.loads(price_server.get_price("unobtainium"))
    assert bad_name["error"]


def test_get_trend_requires_enough_observations() -> None:
    payload = json.loads(price_server.get_trend("lithium_carbonate", 30))
    assert payload["data"]["observations"] >= 2
    assert payload["data"]["series_sample"]
    assert payload["data"]["unit"] == "USD/t"


def test_list_commodities_exposes_units_and_proxy_flags() -> None:
    payload = json.loads(price_server.list_commodities())
    commodities = {item["commodity"] for item in payload["data"]["commodities"]}
    assert {"copper", "lithium_carbonate", "spodumene_sc6", "iron_ore_62fe"} <= commodities
    for item in payload["data"]["commodities"]:
        assert item["unit"]
        if item["is_proxy"]:
            assert item["proxy_note"]
