"""Critic：对成稿前的结构化证据做交叉复核并评分（1-10）。

复用的是题 #3 的核心思想 ——「抽取结果必须被另一个视角挑刺，评分不达标就不许硬发」。
这里的 Critic 是**规则化的数值与引用核查**（可离线、可复现、不花 token），
而不是让模型自评；模型自评容易自我肯定，规则核查才能抓住真问题。

评分协议（与 agent/loop.py 的 Revise Loop 约定一致）：
- 起始 10 分，按问题严重度扣分：critical -2.5 / major -1.2 / minor -0.4
- >= 8 分：通过，可出稿
- < 8 分：进入下一轮（重新规划/补数据），默认最多 2 轮
- 仍不达标：照常出稿，但在「数据可靠性」章节显式列出未解决问题
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.loop import Evidence
from servers.common.config import SETTINGS

PASS_SCORE = 8.0
SEVERITY_PENALTY = {"critical": 2.5, "major": 1.2, "minor": 0.4}


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "minor"
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "severity": self.severity, "evidence": self.evidence}


@dataclass
class CriticReport:
    score: float
    issues: list[Issue]
    checks: dict[str, Any]
    passed: bool
    round_index: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "passed": self.passed,
            "round": self.round_index,
            "issues": [issue.to_dict() for issue in self.issues],
            "checks": self.checks,
        }


def review(evidence: Evidence, *, round_index: int = 0) -> CriticReport:
    issues: list[Issue] = []
    checks: dict[str, Any] = {}

    # 1) 数值自洽：Mt × 品位 ≈ 金属量（容差内），否则可能是列错位
    checks["grade_metal_consistency"] = _check_grade_metal(evidence, issues)

    # 2) 抽取状态：abstain / partial 必须被如实呈现，而不是当成 ok
    checks["resource_status"] = _check_resource_status(evidence, issues)

    # 3) 引用完整性：每条新闻必须有 URL，价格必须有数据来源
    checks["citations"] = _check_citations(evidence, issues)

    # 4) 降级标注：任何 degraded 数据都必须在 warnings 里出现
    checks["degradation_disclosed"] = _check_degradation(evidence, issues)

    # 5) 维度覆盖：题面要求的四个维度是否都有内容
    checks["coverage"] = _check_coverage(evidence, issues)

    # 6) 价格趋势自洽：change_pct 与首末价一致
    checks["price_consistency"] = _check_price_consistency(evidence, issues)

    score = 10.0 - sum(SEVERITY_PENALTY.get(issue.severity, 0.4) for issue in issues)
    score = max(0.0, min(score, 10.0))
    return CriticReport(score=score, issues=issues, checks=checks,
                        passed=score >= PASS_SCORE, round_index=round_index)


def _check_grade_metal(evidence: Evidence, issues: list[Issue]) -> dict[str, Any]:
    out: dict[str, Any] = {"checked": 0, "inconsistent": 0}
    resources = (evidence.resources or {}).get("resources") or {}
    for category, row in resources.items():
        if not isinstance(row, dict):
            continue
        tonnes = row.get("tonnes_mt")
        grade = row.get("grade")
        unit = row.get("grade_unit")
        metal = row.get("metal_tonnes")
        if None in (tonnes, grade, unit, metal) or not metal:
            continue
        out["checked"] += 1
        mass_t = float(tonnes) * 1_000_000.0
        if unit == "g/t":
            derived = mass_t * float(grade) / 1_000_000.0
        elif unit == "%":
            derived = mass_t * float(grade) / 100.0
        else:
            continue
        delta = abs(derived - float(metal)) / abs(float(metal)) * 100.0
        if delta > SETTINGS.pdf.tolerance_pct:
            out["inconsistent"] += 1
            issues.append(Issue(
                code="grade_metal_inconsistent",
                message=(f"{category}：Mt×品位反算金属量与表格值相差 {delta:.1f}%，"
                         f"超过 ±{SETTINGS.pdf.tolerance_pct}% 容差，疑似单位换算或列错位"),
                severity="major",
                evidence={"category": category, "derived_tonnes": derived,
                          "table_tonnes": metal, "delta_pct": round(delta, 2)},
            ))
    if out["checked"] == 0:
        issues.append(Issue(
            code="no_numeric_cross_check",
            message="没有可用于交叉验算的储量字段（Mt+品位+金属量），数值可信度无法复核",
            severity="minor"))
    return out


def _check_resource_status(evidence: Evidence, issues: list[Issue]) -> dict[str, Any]:
    status = (evidence.resources or {}).get("status")
    if not evidence.resources:
        issues.append(Issue(
            code="no_resource_data",
            message="未取得任何储量数据，简报的储量章节只能标注为缺失",
            severity="major" if evidence.project and evidence.project.reports else "minor"))
        return {"status": None}
    if status == "abstain":
        issues.append(Issue(
            code="resource_abstained",
            message="储量抽取判处 abstain（证据不足/双路冲突）：简报必须只报状态、不得给数值",
            severity="minor",
            evidence={"status": status}))
    elif status == "partial":
        issues.append(Issue(
            code="resource_partial",
            message="储量抽取为 partial（部分字段缺失）：这些字段不得在简报中给出数值",
            severity="major", evidence={"status": status}))
    return {"status": status, "confidence": (evidence.resources or {}).get("confidence")}


def _check_citations(evidence: Evidence, issues: list[Issue]) -> dict[str, Any]:
    missing = [item.get("title") for item in evidence.news if not item.get("url")]
    if missing:
        issues.append(Issue(
            code="missing_news_url",
            message=f"{len(missing)} 条新闻缺少可引用 URL，将无法溯源",
            severity="major", evidence={"titles": missing[:3]}))
    uncited_prices = [p.get("commodity") for p in evidence.prices
                      if not p.get("data_origin")]
    if uncited_prices:
        issues.append(Issue(
            code="price_without_source",
            message=f"以下品种的价格缺少数据来源标注：{uncited_prices}",
            severity="major"))
    if not evidence.news and not evidence.articles:
        issues.append(Issue(
            code="no_news",
            message="没有任何新闻来源，简报将缺少新闻摘要与引用链接",
            severity="major"))
    return {"news_items": len(evidence.news), "articles": len(evidence.articles),
            "missing_urls": len(missing), "price_series": len(evidence.prices)}


def _check_degradation(evidence: Evidence, issues: list[Issue]) -> dict[str, Any]:
    if not evidence.degraded:
        return {"degraded": False, "disclosed": True}
    disclosed = any(
        ("离线" in warning or "降级" in warning or "代理" in warning
         or "synthetic" in warning.lower() or "合成" in warning)
        for warning in evidence.warnings
    )
    if not disclosed:
        issues.append(Issue(
            code="degradation_not_disclosed",
            message="存在降级数据（缓存/离线合成/代理指标），但未在告警中明确标注",
            severity="critical"))
        return {"degraded": True, "disclosed": False}
    return {"degraded": True, "disclosed": True}


def _check_coverage(evidence: Evidence, issues: list[Issue]) -> dict[str, Any]:
    coverage = {
        "news": bool(evidence.news or evidence.articles),
        "resources": bool((evidence.resources or {}).get("resources")),
        "price": bool(evidence.prices),
        "risk": bool(evidence.project.risk_notes if evidence.project else True),
    }
    missing = [key for key, present in coverage.items() if not present]
    for key in missing:
        issues.append(Issue(
            code=f"missing_dimension_{key}",
            message=f"题面要求的维度「{key}」没有数据，必须在简报中如实标注为缺失",
            severity="major" if key in ("news", "price") else "minor"))
    return coverage


def _check_price_consistency(evidence: Evidence, issues: list[Issue]) -> dict[str, Any]:
    checked = 0
    inconsistent = 0
    for series in evidence.prices:
        first, last = series.get("start_price"), series.get("end_price")
        change = series.get("change_pct")
        if first is None or last is None or change is None or not first:
            continue
        checked += 1
        derived = (float(last) - float(first)) / float(first) * 100.0
        if abs(derived - float(change)) > 0.5:
            inconsistent += 1
            issues.append(Issue(
                code="price_change_mismatch",
                message=(f"{series.get('commodity')}：涨跌幅 {change}% 与首末价反算 "
                         f"{derived:.2f}% 不一致"),
                severity="major"))
    return {"checked": checked, "inconsistent": inconsistent}
