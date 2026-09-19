"""项目目录（project registry）：把「自然语言主题」映射到可执行的数据源。

Agent 的规划器需要知道：用户说 "Pilbara 锂矿" 时，
- 该查哪些新闻关键词；
- 该读哪份 NI 43-101 报告（mineral-pdf-mcp 的输入）；
- 该看哪些品种的行情（lme-price-mcp 的输入）。

这份目录默认内置若干**合成样例条目**（data/fixtures/projects.json，指向合成 PDF），
生产环境把它换成真实矿权清单即可 —— Agent 编排逻辑不需要改动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from servers.common.config import FIXTURE_DIR, ensure_dirs

PROJECT_INDEX = FIXTURE_DIR / "projects.json"


@dataclass
class ProjectEntry:
    key: str
    name: str
    aliases: tuple[str, ...]
    country: str
    commodity: str
    news_keywords: tuple[str, ...]
    reports: tuple[dict[str, Any], ...]
    price_commodities: tuple[str, ...]
    risk_notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name, "aliases": list(self.aliases),
            "country": self.country, "commodity": self.commodity,
            "news_keywords": list(self.news_keywords),
            "reports": list(self.reports),
            "price_commodities": list(self.price_commodities),
            "risk_notes": list(self.risk_notes),
        }


_DEFAULT_PROJECTS: list[dict[str, Any]] = [
    {
        "key": "pilbara-lithium",
        "name": "Pilbara Lithium (Pilgangoora)",
        "aliases": ["pilbara", "pilgangoora", "pilbara minerals", "皮尔巴拉", "锂辉石"],
        "country": "AU",
        "commodity": "lithium",
        "news_keywords": ["Pilbara lithium", "spodumene", "Pilbara Minerals",
                          "锂 澳洲 出口"],
        "reports": [
            {
                "label": "Pilbara-style NI 43-101 (SYNTHETIC)",
                "path": "pilbara_ni43-101_synthetic.pdf",
                "synthetic": True,
                "note": "合成样例：表格跨页 + 某分类品位缺失（用于验证 abstain 行为）",
            }
        ],
        "price_commodities": ["lithium_carbonate", "spodumene_sc6"],
        "risk_notes": [
            "锂价波动率高，储量经济性假设对价格敏感",
            "澳洲关键矿产外资审查与出口政策变动",
            "中游转化产能集中度带来的议价风险",
        ],
    },
    {
        "key": "ahafo-north-gold",
        "name": "Ahafo North (Gold, Ghana)",
        "aliases": ["ahafo", "newmont", "加纳 金矿", "gold ghana"],
        "country": "GH",
        "commodity": "gold",
        "news_keywords": ["Newmont Ghana", "gold mine West Africa", "加纳 金矿"],
        "reports": [
            {
                "label": "Newmont-style NI 43-101 (SYNTHETIC)",
                "path": "newmont_ni43-101_synthetic.pdf",
                "synthetic": True,
                "note": "合成样例：Inferred 以含于 Indicated 的增量形式出现",
            }
        ],
        "price_commodities": ["gold"],
        "risk_notes": [
            "单一司法辖区政治与财税风险",
            "Inferred 资源不得视同储量，经济性未证实",
        ],
    },
    {
        "key": "rekodiq-copper-gold",
        "name": "Reko Diq-style Cu-Au (Pakistan)",
        "aliases": ["rekodiq", "reko diq", "barrick", "铜金 斑岩"],
        "country": "PK",
        "commodity": "copper",
        "news_keywords": ["Reko Diq copper", "Barrick copper gold", "斑岩 铜"],
        "reports": [
            {
                "label": "Barrick-style NI 43-101 (SYNTHETIC)",
                "path": "barrick_ni43-101_synthetic.pdf",
                "synthetic": True,
                "note": "合成样例：品位列存在 CuEq 等价品位干扰 + Mlb 单位",
            }
        ],
        "price_commodities": ["copper", "gold"],
        "risk_notes": [
            "冶炼加工费（TC/RC）走低压缩冶炼与精矿经济性",
            "跨境司法辖区与项目融资结构复杂",
        ],
    },
]


def ensure_project_index() -> Path:
    ensure_dirs()
    if not PROJECT_INDEX.exists():
        PROJECT_INDEX.write_text(
            json.dumps({"generated": True, "projects": _DEFAULT_PROJECTS},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return PROJECT_INDEX


def load_projects() -> list[ProjectEntry]:
    payload = json.loads(ensure_project_index().read_text(encoding="utf-8"))
    entries: list[ProjectEntry] = []
    for item in payload.get("projects", []):
        entries.append(ProjectEntry(
            key=item["key"],
            name=item["name"],
            aliases=tuple(item.get("aliases") or ()),
            country=item.get("country", ""),
            commodity=item.get("commodity", ""),
            news_keywords=tuple(item.get("news_keywords") or ()),
            reports=tuple(item.get("reports") or ()),
            price_commodities=tuple(item.get("price_commodities") or ()),
            risk_notes=tuple(item.get("risk_notes") or ()),
        ))
    return entries


def resolve_project(topic: str) -> ProjectEntry | None:
    """按别名/名称做最左最长匹配，命中不到时返回 None（由调用方决定是否泛化处理）。"""
    text = (topic or "").lower()
    if not text:
        return None
    best: tuple[int, ProjectEntry] | None = None
    for entry in load_projects():
        candidates = [entry.key, entry.name.lower(), *entry.aliases]
        for candidate in candidates:
            if candidate and candidate in text:
                score = len(candidate)
                if best is None or score > best[0]:
                    best = (score, entry)
    return best[1] if best else None


def report_path(entry: ProjectEntry, report: dict[str, Any]) -> str:
    """把目录里的相对文件名解析成绝对路径（mineral-pdf-mcp 允许读取的目录内）。"""
    raw = str(report.get("path") or "")
    candidate = Path(raw)
    return str(candidate if candidate.is_absolute() else FIXTURE_DIR / candidate)
