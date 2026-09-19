"""任务书要求的三个 server 是否真的暴露了指定工具（MCP 层端到端握手）。

这条测试是交付清单里「3 个 MCP server + 必需工具」的直接对应物：
它真的把 server 作为子进程拉起来，走完 initialize → tools/list 才判定。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.mcp_registry import build_clients, spec_by_key      # noqa: E402

REQUIRED_TOOLS = {
    "mining-news-mcp": {"search", "fetch_article"},
    "mineral-pdf-mcp": {"extract_resources"},
    "lme-price-mcp": {"get_price", "get_trend"},
}


@pytest.mark.parametrize("server_key", sorted(REQUIRED_TOOLS))
def test_server_exposes_required_tools(server_key: str) -> None:
    spec = spec_by_key(server_key)
    client = build_clients([spec])[0]
    try:
        client.start()
        assert client.server_info.get("protocolVersion")
        for tool in REQUIRED_TOOLS[server_key]:
            assert tool in client.tools, f"{server_key} 缺少必需工具 {tool}"
        for tool in client.tools.values():
            schema_ = tool.input_schema
            assert schema_.get("type") == "object"
            # 每个必需参数都必须在 properties 里声明，否则客户端无法构造调用
            for required in schema_.get("required") or []:
                assert required in (schema_.get("properties") or {})
    finally:
        client.close()


def test_tool_call_returns_structured_content() -> None:
    spec = spec_by_key("lme-price-mcp")
    client = build_clients([spec])[0]
    try:
        client.start()
        result = client.call_tool("list_commodities", {})
        assert result.ok and not result.is_error
        assert isinstance(result.data, dict)
        assert result.data["tool"] == "lme-price-mcp.list_commodities"
        assert result.data["data"]["commodities"]
    finally:
        client.close()


def test_unknown_tool_reports_error_without_killing_server() -> None:
    spec = spec_by_key("lme-price-mcp")
    client = build_clients([spec])[0]
    try:
        client.start()
        bad = client.call_tool("not_a_tool", {})
        assert not bad.ok
        # server 必须还活着，后续正常调用仍可用
        good = client.call_tool("list_commodities", {})
        assert good.ok
    finally:
        client.close()


def test_bad_arguments_surface_as_error_result() -> None:
    spec = spec_by_key("lme-price-mcp")
    client = build_clients([spec])[0]
    try:
        client.start()
        result = client.call_tool("get_price", {"commodity": "unobtainium"})
        assert result.ok                       # 工具自身用信封表达业务错误
        assert result.data["error"]
        assert result.data["provenance"]["degraded"] is True
    finally:
        client.close()
