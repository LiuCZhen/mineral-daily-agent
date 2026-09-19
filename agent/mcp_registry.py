"""MCP server 注册表：Agent 与验证脚本共用的唯一事实来源。

每个 server 以子进程方式拉起，命令固定为 `python -m <module>`，
因此 mcp-config.json（给 Claude Desktop / Cursor）与 Agent 内部编排用的是同一套命令，
不存在「配置文件里能跑、Agent 里跑不了」的偏差。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServerSpec:
    key: str                       # mcp-config.json 里的名字
    module: str                    # python -m <module>
    title: str
    tools: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 120.0

    def command(self, python: str | None = None) -> list[str]:
        return [python or sys.executable, "-m", self.module]


SPECS: tuple[ServerSpec, ...] = (
    ServerSpec(
        key="mining-news-mcp",
        module="servers.mining_news_mcp.server",
        title="矿业新闻聚合（RSS + 全文抽取）",
        tools=("search", "fetch_article", "list_sources", "ingest"),
    ),
    ServerSpec(
        key="mineral-pdf-mcp",
        module="servers.mineral_pdf_mcp.server",
        title="NI 43-101 储量抽取（PDF → Indicated/Inferred）",
        tools=("extract_resources", "verify_extraction", "list_sample_reports"),
        timeout_s=180.0,
    ),
    ServerSpec(
        key="lme-price-mcp",
        module="servers.lme_price_mcp.server",
        title="金属价格行情（现货/期货日线 + 趋势统计）",
        tools=("get_price", "get_trend", "list_commodities"),
    ),
)


def server_specs() -> tuple[ServerSpec, ...]:
    return SPECS


def spec_by_key(key: str) -> ServerSpec:
    for spec in SPECS:
        if spec.key == key:
            return spec
    raise KeyError(key)


def build_clients(specs: tuple[ServerSpec, ...] | None = None, *,
                  python: str | None = None, extra_env: dict[str, str] | None = None):
    """构造（未启动的）MCP 客户端列表。"""
    from servers.common.mcp_client import McpClient

    clients = []
    for spec in specs or SPECS:
        env = dict(spec.env)
        if extra_env:
            env.update(extra_env)
        clients.append(McpClient(
            name=spec.key,
            command=spec.command(python),
            cwd=ROOT,
            env=env,
            timeout_s=spec.timeout_s,
        ))
    return clients


def mcp_config(python: str | None = None, *, root: Path | None = None) -> dict:
    """生成可直接粘贴进 Claude Desktop / Cursor 的 mcpServers 配置。"""
    base = Path(root or ROOT)
    servers: dict[str, dict] = {}
    for spec in SPECS:
        entry: dict[str, object] = {
            "command": python or sys.executable,
            "args": ["-m", spec.module],
            "cwd": str(base),
        }
        if spec.env:
            entry["env"] = dict(spec.env)
        servers[spec.key] = entry
    return {"mcpServers": servers}
