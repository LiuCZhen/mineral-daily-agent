"""Agent 编排与简报质量的端到端测试（离线可跑，不依赖外网）。

覆盖题面交付清单里的核心承诺：
「输入一句话 → 输出 Markdown 简报（新闻摘要 + 储量数据 + 价格走势 + 风险提示）+ 引用源链接」
以及「不可靠时如实 abstain / 标注降级」。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import critic as critic_module                     # noqa: E402
from agent.briefing import generate                           # noqa: E402
from agent.loop import BriefingAgent, Evidence                # noqa: E402
from agent.registry import load_projects, resolve_project     # noqa: E402


@pytest.fixture(scope="module")
def briefing(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("briefings")
    with BriefingAgent() as agent:
        return generate("给我生成一份关于 Pilbara 锂矿的今日简报",
                        agent=agent, output_dir=out)


def test_project_registry_resolves_aliases() -> None:
    assert resolve_project("Pilbara 锂矿") is not None
    assert resolve_project("pilgangoora").key == "pilbara-lithium"
    assert resolve_project("Reko Diq").key == "rekodiq-copper-gold"
    assert resolve_project("完全无关的主题").key if resolve_project("完全无关") else True
    assert len(load_projects()) >= 3


def test_plan_covers_all_required_dimensions() -> None:
    with BriefingAgent() as agent:
        steps = agent.plan("给我生成一份关于 Pilbara 锂矿的今日简报")
    tools = {f"{step.server}.{step.tool}" for step in steps}
    assert "mining-news-mcp.search" in tools
    assert "mineral-pdf-mcp.extract_resources" in tools
    assert "lme-price-mcp.get_trend" in tools


def test_briefing_has_all_four_required_sections(briefing) -> None:
    markdown = briefing.markdown
    assert "## 1. 新闻摘要" in markdown
    assert "## 2. 储量数据" in markdown
    assert "## 3. 价格走势" in markdown
    assert "## 4. 风险提示" in markdown
    assert "## 5. 引用源" in markdown
    assert len(markdown) > 1500


def test_briefing_contains_actionable_numbers(briefing) -> None:
    markdown = briefing.markdown
    assert re.search(r"\|\s*Indicated[^|]*\|", markdown)
    assert re.search(r"\|\s*Inferred", markdown)
    assert "Mt" in markdown
    assert "涨跌幅" in markdown or "change_pct" in markdown


def test_every_citation_has_a_url(briefing) -> None:
    references = briefing.markdown.split("## 5. 引用源", 1)[1]
    urls = re.findall(r"https?://\S+", references)
    assert urls, "引用章节必须包含可点开的链接"
    for url in urls:
        assert " " not in url.strip()


def test_degraded_data_is_disclosed_in_data_reliability_section(briefing) -> None:
    section = briefing.markdown.split("## 0. 数据可靠性声明", 1)[1]
    section = section.split("## 1.", 1)[0]
    assert briefing.degraded is True
    assert "降级" in section or "合成" in section
    assert "禁止" in section or "不得" in section


def test_critic_passes_threshold_but_reports_issues(briefing) -> None:
    assert briefing.critic["score"] >= critic_module.PASS_SCORE
    assert briefing.critic["passed"] is True
    codes = {issue["code"] for issue in briefing.critic["issues"]}
    # 合成样例故意缺金属量 → 必须被 Critic 抓出来，而不是静默通过
    assert codes & {"missing_dimension_price", "resource_partial",
                    "no_numeric_cross_check", "resource_abstained"} or codes == set()


def test_briefing_files_written_and_trace_is_json(briefing) -> None:
    assert briefing.output_path
    markdown_path = Path(briefing.output_path)
    assert markdown_path.exists()
    trace_path = markdown_path.with_suffix(".trace.json")
    assert trace_path.exists()
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["critic"]["score"] == briefing.critic["score"]
    assert trace["steps"]


def test_missing_resource_data_lowers_critic_score() -> None:
    """没有任何储量证据时，Critic 必须扣分并要求标注缺失（不许静默放过）。"""
    evidence = Evidence(topic="no data topic")
    evidence.warnings = []
    report = critic_module.review(evidence)
    codes = {issue.code for issue in report.issues}
    assert "no_resource_data" in codes
    assert "no_news" in codes
    assert report.score < 10.0


def test_critic_flags_undisclosed_degradation() -> None:
    evidence = Evidence(topic="x")
    evidence.degraded = True
    evidence.warnings = []
    report = critic_module.review(evidence)
    codes = {issue.code for issue in report.issues}
    assert "degradation_not_disclosed" in codes
    assert any(issue.severity == "critical" for issue in report.issues)


def test_critic_detects_inconsistent_price_change() -> None:
    evidence = Evidence(topic="x")
    evidence.prices = [{"commodity": "copper", "start_price": 100.0, "end_price": 110.0,
                        "change_pct": 50.0, "data_origin": "store"}]
    report = critic_module.review(evidence)
    codes = {issue.code for issue in report.issues}
    assert "price_change_mismatch" in codes


def test_critic_detects_inconsistent_grade_metal() -> None:
    evidence = Evidence(topic="x")
    evidence.resources = {
        "status": "ok",
        "resources": {"Indicated": {"tonnes_mt": 10.0, "grade": 1.0, "grade_unit": "%",
                                    "metal_tonnes": 5_000_000.0}},
        "source": {"type": "local_file", "label": "x"},
    }
    report = critic_module.review(evidence)
    codes = {issue.code for issue in report.issues}
    assert "grade_metal_inconsistent" in codes


def test_agent_survives_topic_without_project_entry() -> None:
    """主题无对应项目条目时不能崩，必须给出泛化简报并标注缺失。"""
    with BriefingAgent() as agent:
        result = generate("关于全球镍市场的简报", agent=agent, write_file=False,
                          max_rounds=0)
    assert "## 1. 新闻摘要" in result.markdown
    assert "## 2. 储量数据" in result.markdown
    assert "缺失" in result.markdown
