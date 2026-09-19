"""简报渲染：把结构化证据变成 Markdown（题面要求的最终输出）。

硬规则（渲染层的唯一职责边界）：
- 所有数字只能来自工具返回的结构化字段，渲染器不做任何推算或补全；
- 缺失 / abstain / partial / 降级一律显式写出，不用 0 或估算值填充；
- 每条新闻、每份报告、每个价格序列都带可点开的引用编号。
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from agent.critic import CriticReport
from agent.loop import Evidence

_STATUS_TEXT = {
    "ok": "✅ 已核对",
    "partial": "⚠️ 部分字段缺失",
    "abstain": "⛔ 已弃权（abstain）",
    None: "— 未取到数据",
}

_GRADE_DISPLAY = {"%": "%", "g/t": "g/t", "ppm": "ppm"}


def render(evidence: Evidence, critic: CriticReport, *,
           plan: list[dict[str, Any]] | None = None,
           extra_notes: list[str] | None = None) -> str:
    buf = io.StringIO()
    write = buf.write
    project = evidence.project
    subject = project.name if project else evidence.topic

    write(f"# 矿权日报 — {subject}\n\n")
    write(f"- **生成时间**：{evidence.as_of}\n")
    write(f"- **主题**：{evidence.topic}\n")
    if project:
        write(f"- **所属国家/地区**：{project.country}\n")
        write(f"- **主要品种**：{project.commodity}\n")
    write(f"- **数据可靠性**：{'⚠️ 存在降级数据' if evidence.degraded else '✅ 一手数据'} "
          f"（自检评分 {critic.score:.1f}/10，"
          f"{'通过' if critic.passed else '未达 8 分阈值'}）\n")
    write(f"- **工具调用**：{len(evidence.steps)} 步，"
          f"成功 {sum(1 for s in evidence.steps if s.status == 'ok')}，"
          f"失败 {sum(1 for s in evidence.steps if s.status == 'error')}，"
          f"跳过 {sum(1 for s in evidence.steps if s.status == 'skipped')}\n\n")

    # ---------------------------------------------------------- 数据可靠性
    _render_reliability(write, evidence, critic, extra_notes or [])

    # -------------------------------------------------------------- 新闻
    _render_news(write, evidence)

    # -------------------------------------------------------------- 储量
    _render_resources(write, evidence)

    # -------------------------------------------------------------- 价格
    _render_prices(write, evidence)

    # -------------------------------------------------------------- 风险
    _render_risks(write, evidence, critic)

    # -------------------------------------------------------------- 引用
    _render_references(write, evidence)

    # -------------------------------------------------------- 方法与可复现
    _render_method(write, evidence, plan or [step.to_dict() for step in evidence.steps])
    return buf.getvalue()


def _render_reliability(write: Any, evidence: Evidence, critic: CriticReport,
                        extra_notes: list[str]) -> None:
    write("## 0. 数据可靠性声明\n\n")
    rows: list[str] = []
    if evidence.degraded:
        rows.append("本简报包含**降级数据**：部分数据来自缓存、离线合成序列或代理指标，"
                    "不得作为交易或披露依据。")
    else:
        rows.append("本次所有数据均来自一手抓取或本地报告的实时解析。")
    for warning in dict.fromkeys(evidence.warnings):
        rows.append(f"- {warning}")
    for note in extra_notes:
        rows.append(f"- {note}")
    if not evidence.warnings and not extra_notes:
        rows.append("- 无异常告警。")
    for row in rows:
        write(f"{row}\n" if row.startswith("-") else f"{row}\n\n")

    unresolved = [issue for issue in critic.issues if issue.severity in ("critical", "major")]
    if unresolved:
        write("\n**未解决的自检问题（Critic 判定）**：\n\n")
        for issue in unresolved:
            write(f"- `{issue.code}`（{issue.severity}）：{issue.message}\n")
    write("\n")


def _render_news(write: Any, evidence: Evidence) -> None:
    write("## 1. 新闻摘要\n\n")
    if not evidence.news and not evidence.articles:
        write("_未检索到相关新闻（数据缺失，本节仅标注状态，不做推测）。_\n\n")
        return

    fulltext = evidence.articles[0] if evidence.articles else None
    for index, item in enumerate(evidence.news, start=1):
        flags = []
        if item.get("_degraded"):
            flags.append("降级数据")
        meta = " · ".join(filter(None, [
            item.get("source"), item.get("published_at"),
            ("，".join(item.get("commodities") or []) or None),
            ("，".join(item.get("regions") or []) or None),
        ]))
        title = item.get("title") or "(无标题)"
        write(f"{index}. **{title}** — [{_host(item.get('url'))}]({item.get('url')})"
              + (f"（{'; '.join(flags)}）" if flags else "") + "\n")
        if meta:
            write(f"   - {meta}\n")
        summary = (item.get("summary") or "").strip().replace("\n", " ")
        if summary:
            write(f"   - 摘要：{summary[:320]}\n")
        if item.get("match_terms"):
            write(f"   - 命中词：{', '.join(item['match_terms'])}\n")
    write("\n")

    if fulltext:
        write(f"**重点文章全文要点**（{fulltext.get('source') or ''}"
              f"{'，降级数据' if fulltext.get('_degraded') else ''}）：\n\n")
        write(f"> {fulltext.get('title')}\n>\n")
        completeness = fulltext.get("content_completeness")
        if completeness and completeness != "full":
            write(f"> ⚠️ 正文完整度：`{completeness}`\n>\n")
        if fulltext.get("paywall_detected"):
            write("> ⚠️ 该页面存在付费墙/登录墙，正文可能不完整。\n>\n")
        excerpt = (fulltext.get("content") or "").strip()
        title_text = (fulltext.get("title") or "").strip()
        paragraphs = []
        for paragraph in excerpt.split("\n\n"):
            cleaned = paragraph.strip()
            # HTML 抽取会把页面 h1（与标题重复）带进来，渲染时去掉，避免重复引用
            if cleaned and cleaned != title_text and not cleaned.startswith(title_text[:40]):
                paragraphs.append(cleaned)
            if len(paragraphs) >= 4:
                break
        for paragraph in paragraphs:
            write(f"> {paragraph[:800]}\n>\n")
        write("\n")


def _render_resources(write: Any, evidence: Evidence) -> None:
    write("## 2. 储量数据（NI 43-101 Indicated / Inferred）\n\n")
    resources = evidence.resources or {}
    status = resources.get("status")
    rows = resources.get("resources") or {}
    stats = resources.get("stats") or {}
    source = resources.get("source") or {}

    write(f"- 抽取状态：{_STATUS_TEXT.get(status, status)}"
          f"（置信度 {resources.get('confidence')}）\n")
    if source:
        write(f"- 数据来源：`{source.get('type')}` — {source.get('label')}"
              f"（{source.get('pages')} 页）\n")
    if stats:
        write(f"- 解析统计：候选行 {stats.get('rows_found')} 行，"
              f"识别分类 {', '.join(stats.get('categories_found') or []) or '无'}，"
              f"页内扫描 {stats.get('pages_scanned')}/{stats.get('pages_total')} 页，"
              f"品位×矿量一致率 "
              f"{stats.get('grade_metal_consistency_rate')}\n")
    write("\n")

    if status == "abstain":
        write("> ⛔ **系统已弃权（abstain）**：该报告未能提供足以支撑结论的证据。"
              "本节不给出任何数值，请人工复核原始报告。\n\n")
        return
    if not rows:
        write("_未取得储量数据（该项目在目录中没有对应报告，或报告解析失败）。_\n\n")
        return

    write("| 分类 | 矿石量 (Mt) | 品位 | 金属量 | 反算金属吨 | 页码 |\n")
    write("| --- | ---: | ---: | ---: | ---: | ---: |\n")
    for category in ("Indicated", "Inferred"):
        row = rows.get(category)
        if not row:
            write(f"| {category} | 缺失 | 缺失 | 缺失 | 缺失 | — |\n")
            continue
        tonnes = _fmt(row.get("tonnes_mt"), 2)
        grade = "缺失" if row.get("grade") is None else (
            f"{_fmt(row.get('grade'), 2)} "
            f"{_GRADE_DISPLAY.get(row.get('grade_unit'), row.get('grade_unit') or '')}")
        metal = "缺失" if row.get("metal") is None else (
            f"{_fmt(row.get('metal'), 0)} {row.get('metal_unit') or ''}".strip())
        derived = _fmt(row.get("metal_tonnes"), 2)
        note = ""
        if row.get("included_in"):
            note = f"（含于 {row['included_in']} 之内）"
        write(f"| {category}{note} | {tonnes} | {grade} | {metal} | {derived} | "
              f"{row.get('page') or '—'} |\n")
    write("\n")

    nested = resources.get("nested") or []
    if nested:
        write("**嵌套说明**：\n\n")
        for row in nested:
            write(f"- {row.get('category')} 的 {_fmt(row.get('tonnes_mt'), 2)} Mt "
                  f"标注为含于 {row.get('included_in')} 之内，不应与其相加。\n")
        write("\n")

    totals = resources.get("totals") or []
    if totals:
        write("**报告中的合计行（原样引用，未参与本表格计算）**：\n\n")
        for row in totals:
            write(f"- {row.get('category')}：{_fmt(row.get('tonnes_mt'), 2)} Mt @ "
                  f"{_fmt(row.get('grade_value'), 2)} {row.get('grade_unit') or ''} → "
                  f"{_fmt(row.get('metal_value'), 0)} {row.get('metal_unit') or ''}\n")
        write("\n")

    for warning in (evidence.resource_meta or {}).get("warnings") or []:
        write(f"> ⚠️ {warning}\n")
    if (evidence.resource_meta or {}).get("warnings"):
        write("\n")


def _render_prices(write: Any, evidence: Evidence) -> None:
    write("## 3. 价格走势（近 30 天）\n\n")
    if not evidence.prices:
        write("_未取得任何价格数据（数据缺失，本节仅标注状态）。_\n\n")
        return

    write("| 品种 | 起止日期 | 起始 | 最新 | 涨跌幅 | 区间低/高 | 年化波动率 | 方向 | 数据来源 |\n")
    write("| --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- |\n")
    for series in evidence.prices:
        low = series.get("min") or {}
        high = series.get("max") or {}
        change = series.get("change_pct")
        origin = series.get("data_origin") or "未知"
        flags = []
        if series.get("_degraded"):
            flags.append("降级")
        if series.get("is_proxy"):
            flags.append("代理指标")
        origin_text = origin + (f"（{'/'.join(flags)}）" if flags else "")
        write(f"| {series.get('display_name') or series.get('commodity')} "
              f"| {series.get('start_date')} → {series.get('end_date')} "
              f"| {_fmt(series.get('start_price'), 2)} | {_fmt(series.get('end_price'), 2)} "
              f"| {'—' if change is None else f'{change:+.2f}%'} "
              f"| {_fmt(low.get('price'), 2)} / {_fmt(high.get('price'), 2)} "
              f"| {_fmt(series.get('annualised_volatility_pct'), 1)}% "
              f"| {_direction_cn(series.get('direction'))} | {origin_text} |\n")
    write("\n")

    proxied = [s for s in evidence.prices if s.get("is_proxy")]
    if proxied:
        write("**代理指标说明**：\n\n")
        for series in proxied:
            write(f"- {series.get('display_name')}：{series.get('proxy_note')}\n")
        write("\n")


def _render_risks(write: Any, evidence: Evidence, critic: CriticReport) -> None:
    write("## 4. 风险提示\n\n")
    risks: list[tuple[str, str]] = []

    for series in evidence.prices:
        change = series.get("change_pct")
        volatility = series.get("annualised_volatility_pct")
        name = series.get("display_name") or series.get("commodity")
        if change is not None and abs(change) >= 5:
            risks.append((f"{name} 价格近 30 天{_direction_cn(series.get('direction'))}"
                          f" {change:+.2f}%，需关注对项目经济性的影响。",
                          "价格波动"))
        if volatility is not None and volatility >= 35:
            risks.append((f"{name} 年化波动率 {volatility:.1f}%，"
                          f"储量估值对价格假设高度敏感。", "价格波动"))

    resources = evidence.resources or {}
    rows = resources.get("resources") or {}
    if resources.get("status") == "abstain":
        risks.append(("储量数据未通过系统自检（abstain），"
                      "任何基于储量的推断都不成立，需人工复核原始报告。", "数据完整性"))
    for category, row in rows.items():
        if isinstance(row, dict) and row.get("included_in"):
            risks.append((f"{category} 资源量以含于 {row['included_in']} 的增量形式披露，"
                          f"不可与上级分类直接相加。", "披露口径"))
        if isinstance(row, dict) and row.get("metal") is None:
            risks.append((f"{category} 缺少金属量字段，无法验证品位与矿量的自洽性。",
                          "数据完整性"))

    if evidence.project:
        for note in evidence.project.risk_notes:
            risks.append((note, "项目层面"))

    for warning in dict.fromkeys(evidence.warnings):
        if "离线" in warning or "合成" in warning or "缓存" in warning:
            risks.append((f"数据可靠性：{warning}", "数据可靠性"))

    if not risks:
        risks.append(("本次未识别出显著风险信号，"
                      "但缺数据本身即为风险，建议补充更多来源后复核。", "数据完整性"))

    for text, tag in risks:
        write(f"- **[{tag}]** {text}\n")
    write("\n")


def _render_references(write: Any, evidence: Evidence) -> None:
    write("## 5. 引用源\n\n")
    index = 1
    if evidence.news:
        write("**新闻**\n\n")
        for item in evidence.news:
            write(f"[{index}] {item.get('title')} — {item.get('source')}"
                  f"（{item.get('published_at') or '时间未知'}）：{item.get('url')}\n\n")
            index += 1
    for article in evidence.articles:
        write(f"[{index}] 全文：{article.get('title')} — {article.get('url')}\n\n")
        index += 1
    resources = evidence.resources or {}
    source = resources.get("source") or {}
    if source:
        write(f"[{index}] NI 43-101 报告：{source.get('label')}"
              f"（{source.get('type')}，{source.get('pages')} 页）\n\n")
        index += 1
    if evidence.prices:
        write(f"[{index}] 价格序列来源："
              f"{', '.join(sorted({str(p.get('data_origin')) for p in evidence.prices}))}"
              f"（单位/代理说明见表内标注）\n\n")


def _render_method(write: Any, evidence: Evidence, plan: list[dict[str, Any]]) -> None:
    write("## 6. 方法与可复现说明\n\n")
    write("本简报由 `mineral-daily-agent` 通过 MCP 协议编排三个 server 生成：\n\n")
    write("| 步骤 | 工具 | 参数 | 状态 | 耗时 |\n| --- | --- | --- | --- | ---: |\n")
    for step in plan:
        args = step.get("arguments") or {}
        rendered = ", ".join(f"{key}={_short(value)}" for key, value in args.items())
        write(f"| {step.get('title')} | `{step.get('tool')}` | {rendered} "
              f"| {step.get('status')} | {step.get('duration_ms')} ms |\n")
    write("\n")
    for step in plan:
        if step.get("status") in ("error", "skipped") and step.get("error"):
            write(f"- `{step.get('step_id')}` 未完成：{step.get('error')}\n")
    write("\n所有数值均直接取自工具返回的结构化字段，渲染层不做推算或补全；"
          "缺失值一律标注为「缺失」，不填 0。\n")


def _host(url: str | None) -> str:
    if not url:
        return "unknown"
    from urllib.parse import urlsplit
    return urlsplit(url).netloc or url


def _fmt(value: Any, digits: int) -> str:
    if value is None:
        return "缺失"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits == 0:
        return f"{number:,.0f}"
    return f"{number:,.{digits}f}"


def _short(value: Any, limit: int = 40) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _direction_cn(direction: str | None) -> str:
    return {"up": "上涨", "down": "下跌", "flat": "持平", "unknown": "未知"}.get(
        direction or "unknown", "未知")
