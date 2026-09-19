"""Briefing 生成管线：规划 → 执行 → Critic 评分 → (≤N 轮补正) → 渲染 → 落盘。

Revise Loop 的取舍（借鉴题 #3 但做了工程化收敛）：
- 题 #3 是「同一份输入的抽取-挑刺-修订」；这里是「证据收集-核验-补采」，
  因为日报的核心失败模式不是抽错字段，而是**某个维度根本没取到数据**。
- 所以修订动作是「定向补采」：对失败/缺失的步骤换参数再试一次（更宽的关键词、
  换品种、放宽时间窗），而不是让模型重写文字。
- 每轮都写 evolution 日志（stage=briefing_revise），题目最后可复跑这条 log 做改进。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent import critic as critic_module
from agent import reporter
from agent.loop import BriefingAgent, Evidence, PlanStep
from servers.common.config import DATA_DIR, SETTINGS, ensure_dirs
from servers.common.llm import LLMClient, LLMUnavailable
from servers.common.logging_utils import get_logger
from servers.common.store import Store

log = get_logger("agent.briefing")


@dataclass
class BriefingResult:
    topic: str
    markdown: str
    critic: dict[str, Any]
    critic_history: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)
    output_path: str | None = None
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "markdown": self.markdown,
            "critic": self.critic,
            "critic_history": self.critic_history,
            "steps": self.steps,
            "used_tools": self.used_tools,
            "output_path": self.output_path,
            "degraded": self.degraded,
            "warnings": self.warnings,
        }


def _revision_steps(evidence: Evidence, report: critic_module.CriticReport) -> list[PlanStep]:
    """根据 Critic 的问题定向生成补采步骤。"""
    codes = {issue.code for issue in report.issues}
    revisions: list[PlanStep] = []

    if codes & {"no_news", "missing_news_url"}:
        keyword = evidence.project.news_keywords[0] if evidence.project else evidence.topic
        revisions.append(PlanStep(
            step_id="revise-news-broaden", title=f"放宽检索：{keyword}（30 天）",
            server="mining-news-mcp", tool="search",
            arguments={"query": keyword, "days": 30, "limit": 8},
            purpose="首轮新闻不足，放宽时间窗与条数重试",
        ))
    if "no_resource_data" in codes and evidence.project:
        for index, report_entry in enumerate(evidence.project.reports, start=1):
            # 已成功抽取过的报告不再重复
            done = any(step.step_id == f"resources-{index}" and step.status == "ok"
                       for step in evidence.steps)
            if done:
                continue
            from agent.registry import report_path
            revisions.append(PlanStep(
                step_id=f"revise-resources-{index}",
                title=f"重试储量抽取：{report_entry.get('label')}",
                server="mineral-pdf-mcp", tool="extract_resources",
                arguments={"pdf_url": report_path(evidence.project, report_entry),
                           "include_raw_rows": True},
                purpose="首轮储量抽取失败，带原始行重试以便复核",
            ))
    if "price_without_source" in codes or not evidence.prices:
        commodities = (evidence.project.price_commodities
                       if evidence.project else ("copper",))
        for index, commodity in enumerate(commodities[:2], start=1):
            revisions.append(PlanStep(
                step_id=f"revise-price-{index}", title=f"补采价格：{commodity}（90 天）",
                server="lme-price-mcp", tool="get_trend",
                arguments={"commodity": commodity, "days": 90},
                purpose="首轮价格缺失，放宽时间窗重试",
            ))
    return revisions


def generate(topic: str, *, agent: BriefingAgent | None = None,
             max_rounds: int | None = None, write_file: bool = True,
             output_dir: Path | None = None) -> BriefingResult:
    """生成简报主入口。agent 为 None 时自行启动并关闭三个 MCP server。"""
    owns_agent = agent is None
    agent = agent or BriefingAgent()
    if owns_agent:
        agent.start()
    store = Store()
    rounds = max_rounds if max_rounds is not None else agent.max_revise_rounds

    try:
        evidence = agent.run(topic)
        report = critic_module.review(evidence, round_index=0)
        history = [report.to_dict()]

        for round_index in range(1, rounds + 1):
            if report.passed:
                break
            revisions = _revision_steps(evidence, report)
            if not revisions:
                break
            log.info("revise round %d with %d steps (score %.2f)",
                     round_index, len(revisions), report.score)
            for step in revisions:
                agent._execute_step(step, evidence)
                evidence.steps.append(step)
            store.log_evolution(
                stage="briefing_revise",
                reason=f"score {report.score:.2f} < {critic_module.PASS_SCORE}",
                tool="briefing",
                severity="warning",
                payload={"round": round_index,
                         "issues": [issue.code for issue in report.issues],
                         "steps": [step.step_id for step in revisions]},
            )
            report = critic_module.review(evidence, round_index=round_index)
            history.append(report.to_dict())

        # 只保留一轮检索结果里重复的条目
        evidence.news = _dedupe_news(evidence.news)

        extra_notes: list[str] = []
        polished = _polish_narrative(evidence, extra_notes)
        markdown = reporter.render(evidence, report, extra_notes=extra_notes)
        if polished:
            markdown = markdown.replace("## 1. 新闻摘要\n",
                                        f"## 1. 新闻摘要\n\n> {polished}\n", 1)

        result = BriefingResult(
            topic=topic,
            markdown=markdown,
            critic=report.to_dict(),
            critic_history=history,
            steps=[step.to_dict() for step in evidence.steps],
            used_tools=list(dict.fromkeys(agent.used_tools)),
            degraded=evidence.degraded,
            warnings=evidence.warnings,
        )
        if write_file:
            result.output_path = str(_write_briefing(result, output_dir))
        store.log_evolution(
            stage="briefing_published",
            reason=f"score {report.score:.2f}",
            tool="briefing", severity="info",
            payload={"topic": topic, "degraded": evidence.degraded,
                     "output": result.output_path,
                     "issues": [issue.code for issue in report.issues]},
        )
        return result
    finally:
        if owns_agent:
            agent.close()


def _dedupe_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda row: row.get("relevance") or 0, reverse=True):
        url = item.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
    return out


def _polish_narrative(evidence: Evidence, extra_notes: list[str]) -> str | None:
    """可选的 LLM 措辞润色：只允许改写文字，禁止引入新数字。

    为控制风险，这里把「允许引用的事实」显式列给模型，并要求不得新增数字；
    返回值只作为摘要引言插入，正文表格仍由渲染器统一生成。
    """
    if not SETTINGS.llm.configured or not evidence.news:
        return None
    headlines = "\n".join(
        f"- {item.get('title')}（{item.get('source')}，{item.get('published_at')}）"
        for item in evidence.news[:6]
    )
    prices = "\n".join(
        f"- {series.get('display_name')}: {series.get('change_pct')}% "
        f"(近 30 天，来源 {series.get('data_origin')})"
        for series in evidence.prices
    )
    prompt = (
        "用中文写 2-3 句简报引言，概括下面的新闻与价格事实。"
        "严格约束：不得出现任何未在材料中给出的数字；不得做出买卖建议；"
        "若材料标注为降级/合成数据，必须在引言中点明。\n\n"
        f"主题：{evidence.topic}\n新闻标题：\n{headlines}\n价格：\n{prices or '（无价格数据）'}\n"
    )
    try:
        response = LLMClient(role="extractor").complete(
            prompt, system="You are a cautious mining-industry briefing writer.")
    except LLMUnavailable as exc:
        extra_notes.append(f"LLM 润色未启用：{exc}")
        return None
    except Exception as exc:                            # noqa: BLE001
        extra_notes.append(f"LLM 润色失败（已回退到模板摘要）：{type(exc).__name__}")
        return None
    text = response.text.strip()
    return text or None


def _write_briefing(result: BriefingResult, output_dir: Path | None) -> Path:
    ensure_dirs()
    target_dir = Path(output_dir or (DATA_DIR / "briefings"))
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"briefing-{_ascii_slug(result.topic)}-{stamp}.md"
    path.write_text(result.markdown, encoding="utf-8")
    (target_dir / f"briefing-{_ascii_slug(result.topic)}-{stamp}.trace.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _ascii_slug(text: str, limit: int = 40) -> str:
    """文件名只保留 ASCII，避免在不同系统/归档工具上出现编码问题。"""
    ascii_only = "".join(ch if (ch.isascii() and ch.isalnum()) else "-" for ch in text.lower())
    slug = "-".join(part for part in ascii_only.split("-") if part)[:limit].strip("-")
    return slug or "topic"
