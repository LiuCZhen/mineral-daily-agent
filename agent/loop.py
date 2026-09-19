"""Agent 主流程（自写 ReAct/计划-执行循环）。

题面要求「Agent 主流程（你自己设计）」。这里的设计取向是**可控的显式编排**，
而不是让模型自由发挥：

1. **Planner**：把「给我生成一份关于 X 的今日简报」拆成有序步骤
   （新闻检索 → 全文抓取 → 储量抽取 → 行情趋势 → 合成）。
   默认用确定性规划器（不依赖 LLM，可离线复现）；配了 key 可切换 LLM 规划。
2. **Executor**：逐步通过 MCP 调用工具，处理失败与降级，把每次异常写入 evolution 日志。
3. **Critic**：对成稿前的结构化证据做交叉复核（数值一致性、降级标注、引用完整性），
   评分 1-10；低于阈值时重新规划一轮（默认 ≤2 轮），仍不达标则在简报中显式标注不确定性。
4. **Renderer**：输出 Markdown 简报（新闻摘要 + 储量数据 + 价格走势 + 风险提示 + 引用源）。

为什么不让模型直接写最终稿：评审要的是「可复核的结论」。
所以模型只在被允许的边界内（摘要措辞、风险归纳）参与，数字一律来自工具返回。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent.mcp_registry import ServerSpec, spec_by_key
from agent.registry import ProjectEntry, report_path, resolve_project
from servers.common.config import SETTINGS
from servers.common.logging_utils import get_logger
from servers.common.mcp_client import CallResult, McpClient
from servers.common.store import Store

log = get_logger("agent.loop")


@dataclass
class PlanStep:
    step_id: str
    title: str
    tool: str
    server: str
    arguments: dict[str, Any]
    purpose: str = ""
    status: str = "pending"          # pending | ok | error | skipped
    result: CallResult | None = None
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "tool": f"{self.server}.{self.tool}",
            "arguments": self.arguments,
            "purpose": self.purpose,
            "status": self.status,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "degraded": self.result.degraded if self.result else None,
        }


@dataclass
class Evidence:
    """一次简报所需的全部结构化证据。"""

    topic: str
    project: ProjectEntry | None = None
    news: list[dict[str, Any]] = field(default_factory=list)
    articles: list[dict[str, Any]] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    resource_meta: dict[str, Any] = field(default_factory=dict)
    prices: list[dict[str, Any]] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False
    as_of: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"))


class BriefingAgent:
    """矿权日报 Agent。"""

    def __init__(self, *, clients: dict[str, McpClient] | None = None,
                 python: str | None = None, tools_limit: int = 8,
                 max_revise_rounds: int = 2) -> None:
        self._clients = clients or {}
        self._python = python
        self.tools_limit = tools_limit
        self.max_revise_rounds = max_revise_rounds
        self.store = Store()
        self.used_tools: list[str] = []

    # ------------------------------------------------------------ 生命周期
    def start(self) -> "BriefingAgent":
        for key in ("mining-news-mcp", "mineral-pdf-mcp", "lme-price-mcp"):
            if key not in self._clients:
                spec = spec_by_key(key)
                self._clients[key] = McpClient(
                    name=spec.key, command=spec.command(self._python),
                    cwd=Path(__file__).resolve().parents[1], timeout_s=spec.timeout_s)
        for client in self._clients.values():
            client.start()
        return self

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def __enter__(self) -> "BriefingAgent":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def catalog(self) -> list[dict[str, Any]]:
        """暴露给 LLM 规划器的工具目录。"""
        out: list[dict[str, Any]] = []
        for key, client in self._clients.items():
            for name, info in sorted(client.tools.items()):
                out.append({"server": key, "tool": name, "description": info.description,
                            "input_schema": info.input_schema})
        return out

    # -------------------------------------------------------------- 规划
    def plan(self, topic: str) -> list[PlanStep]:
        """确定性规划器：稳定、可离线复现，且覆盖题面要求的全部四个维度。"""
        entry = resolve_project(topic)
        keywords = list(entry.news_keywords) if entry else [topic]
        commodities = list(entry.price_commodities) if entry else self._infer_commodities(topic)
        reports = list(entry.reports) if entry else []

        steps: list[PlanStep] = []
        for index, keyword in enumerate(keywords[:3], start=1):
            steps.append(PlanStep(
                step_id=f"news-{index}", title=f"检索新闻：{keyword}",
                server="mining-news-mcp", tool="search",
                arguments={"query": keyword, "days": 14, "limit": 6},
                purpose="收集与主题相关的近期新闻，作为简报的新闻摘要与引用来源",
            ))
        if keywords:
            steps.append(PlanStep(
                step_id="news-fulltext", title="抓取重点文章全文",
                server="mining-news-mcp", tool="fetch_article",
                arguments={"url": "__FROM_NEWS__", "max_chars": 6000},
                purpose="对检索结果中相关度最高的一篇抓取全文，避免只基于标题做摘要",
            ))
        for index, report in enumerate(reports, start=1):
            steps.append(PlanStep(
                step_id=f"resources-{index}", title=f"抽取储量：{report.get('label')}",
                server="mineral-pdf-mcp", tool="extract_resources",
                arguments={"pdf_url": report_path(entry, report) if entry else "",
                           "include_raw_rows": False},
                purpose="取得 Indicated / Inferred 矿石量、品位与金属量，并保留页码证据",
            ))
        for index, commodity in enumerate(commodities[:3], start=1):
            steps.append(PlanStep(
                step_id=f"price-{index}", title=f"价格趋势：{commodity}",
                server="lme-price-mcp", tool="get_trend",
                arguments={"commodity": commodity, "days": 30},
                purpose="给出近 30 天价格方向、波动率与区间高低，支撑风险提示",
            ))
        return steps

    @staticmethod
    def _infer_commodities(topic: str) -> list[str]:
        text = (topic or "").lower()
        mapping = {
            "lithium": ("lithium", "锂", "spodumene"),
            "copper": ("copper", "铜", "cu"),
            "gold": ("gold", "金", "au"),
            "nickel": ("nickel", "镍"),
            "zinc": ("zinc", "锌"),
            "iron_ore": ("iron ore", "铁矿石", "fe"),
        }
        hits = [canonical for canonical, hints in mapping.items()
                if any(hint in text for hint in hints)]
        return hits or ["copper"]

    # -------------------------------------------------------------- 执行
    def run(self, topic: str, *, max_news: int = 6) -> Evidence:
        steps = self.plan(topic)
        evidence = Evidence(topic=topic, project=resolve_project(topic))
        self.used_tools = []

        for index, step in enumerate(steps):
            self._execute_step(step, evidence)
            evidence.steps.append(step)
            if step.step_id == "news-fulltext" and step.status != "ok":
                # 全依赖上一步结果：没有新闻就不必重试
                step.status = step.status if step.status != "pending" else "skipped"

        evidence.news = evidence.news[:max_news]
        return evidence

    def _execute_step(self, step: PlanStep, evidence: Evidence) -> None:
        # news-fulltext 的 URL 来自检索结果（数据依赖，而非硬编码）
        if step.arguments.get("url") == "__FROM_NEWS__":
            if not evidence.news:
                step.status = "skipped"
                step.error = "没有检索到新闻，跳过全文抓取"
                return
            step.arguments = {**step.arguments, "url": evidence.news[0]["url"]}

        if step.server == "mineral-pdf-mcp" and not step.arguments.get("pdf_url"):
            step.status = "skipped"
            step.error = "该主题在项目目录中没有对应的 NI 43-101 报告"
            self.store.log_evolution("plan", step.error, tool=step.tool, severity="info")
            return

        client = self._clients.get(step.server)
        if client is None:
            step.status = "error"
            step.error = f"server 未启动：{step.server}"
            return

        result = client.call_tool(step.tool, step.arguments)
        step.result = result
        step.duration_ms = result.duration_ms
        self.used_tools.append(f"{step.server}.{step.tool}")

        if not result.ok:
            step.status = "error"
            step.error = result.error or "unknown error"
            # 失败必须留痕：这正是 evolution.jsonl 的用途
            self.store.log_evolution(
                stage="tool_call", reason=step.error or "unknown",
                tool=f"{step.server}.{step.tool}", severity="error",
                payload={"arguments": step.arguments, "attempts": result.attempts},
            )
            evidence.warnings.append(
                f"步骤「{step.title}」失败：{step.error}（已在简报中标注该维度缺失）")
            evidence.degraded = True
            return

        step.status = "ok"
        self._collect(step, evidence)

    def _collect(self, step: PlanStep, evidence: Evidence) -> None:
        data = step.result.data if isinstance(step.result.data, dict) else {}
        payload = data.get("data") or {}
        provenance = data.get("provenance") or {}
        if provenance.get("degraded"):
            evidence.degraded = True

        if step.tool == "search":
            for item in payload.get("results") or []:
                item = {**item, "_step": step.step_id,
                        "_degraded": provenance.get("degraded", False)}
                if item.get("url") and not any(
                        existing.get("url") == item["url"] for existing in evidence.news):
                    evidence.news.append(item)
            for warning in data.get("warnings") or []:
                if warning.get("severity") in ("warning", "error"):
                    evidence.warnings.append(f"新闻检索：{warning.get('message')}")
        elif step.tool == "fetch_article":
            article = payload.get("article") or {}
            if article:
                article["_degraded"] = provenance.get("degraded", False)
                evidence.articles.append(article)
        elif step.tool == "extract_resources":
            evidence.resources = {
                "status": payload.get("status"),
                "resources": payload.get("resources") or {},
                "totals": payload.get("totals") or [],
                "nested": payload.get("nested") or [],
                "stats": payload.get("stats") or {},
                "confidence": provenance.get("confidence"),
                "source": (data.get("provenance", {}).get("sources") or [{}])[0],
            }
            evidence.resource_meta = {
                "status": payload.get("status"),
                "cross_validation": payload.get("cross_validation") or {},
                "warnings": [w.get("message") for w in (data.get("warnings") or [])],
            }
            if payload.get("status") == "abstain":
                evidence.warnings.append(
                    "储量抽取判定为 abstain：该报告未给出足够证据，"
                    "简报中只标注状态、不给具体数值")
            for warning in data.get("warnings") or []:
                if warning.get("severity") in ("warning", "error"):
                    evidence.warnings.append(f"储量抽取：{warning.get('message')}")
        elif step.tool == "get_trend":
            evidence.prices.append({**payload, "_degraded": provenance.get("degraded", False)})
            for warning in data.get("warnings") or []:
                if warning.get("code") in ("fallback_source", "proxy_instrument"):
                    evidence.warnings.append(f"价格（{payload.get('commodity')}）："
                                             f"{warning.get('message')}")
