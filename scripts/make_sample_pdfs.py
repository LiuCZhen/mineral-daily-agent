"""生成合成 NI 43-101 样例报告（PDF）+ ground truth JSON。

背景：题面承诺「我们给你 3 份真实 NI 43-101 PDF」。本仓库不附带任何第三方版权文件，
因此提供等价的合成样例：版式、章节结构、单位混用、合计行与嵌套行等**全部难点都复刻**，
用于离线跑通与回归测试。真实 PDF 只要替换 data/fixtures/sources.json 指向即可。

每份样例都刻意植入至少一个「陷阱」，用来验证系统该 abstain 时是否 abstain：
- newmont: Inferred 嵌套在 Indicated 行内（含于关系）
- barrick: 品位列同时出现 % Cu 与 CuEq（等价品位），且金属量用 Mlb
- pilbara: 表格跨页 + 某分类品位缺失（必须 abstain 而不是猜）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许 `python scripts/make_sample_pdfs.py` 直接运行（RUN.md 的写法）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# reportlab 只用于「生成」合成样例。样例 PDF 已随仓库提供，
# 因此缺这个包时给一句可执行的提示，而不是抛一堆 traceback。
try:
    from reportlab.lib import colors                              # noqa: E402
    from reportlab.lib.pagesizes import LETTER                    # noqa: E402
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
    from reportlab.lib.units import inch                          # noqa: E402
    from reportlab.platypus import (                              # noqa: E402
        PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
except ImportError:                                               # pragma: no cover
    _fixtures = ROOT / "data" / "fixtures"
    _existing = sorted(p.name for p in _fixtures.glob("*_ni43-101_synthetic.pdf"))
    print("缺少 reportlab，无法生成合成样例 PDF。\n")
    print("但样例 PDF 已随仓库提供，通常不需要重新生成：")
    if _existing:
        for name in _existing:
            print(f"  - data/fixtures/{name}")
        print(f"  - data/fixtures/ground_truth.json "
              f"({'存在' if (_fixtures / 'ground_truth.json').exists() else '缺失'})")
        print("\n可以直接跑： python -m agent.cli --offline --topic \"Pilbara 锂矿\"")
    else:
        print("  未找到现成样例，需要安装 reportlab 后重试。")
    print("\n如需安装 reportlab：")
    print("  conda install -c conda-forge reportlab -y")
    print("  python -m pip install reportlab -i https://pypi.tuna.tsinghua.edu.cn/simple")
    raise SystemExit(2)

from servers.common.config import FIXTURE_DIR, ensure_dirs        # noqa: E402

# --------------------------------------------------------------------- 数据
# 所有数值均为合成数据，仅用于演示与测试，不代表任何真实矿权信息。
CASES: list[dict] = [
    {
        "id": "newmont",
        "file": "newmont_ni43-101_synthetic.pdf",
        "company": "Newmont-style Gold Corp. (SYNTHETIC)",
        "project": "Ahafo North Style Gold Project, Ghana",
        "commodity": "Au",
        "grade_unit": "g/t",
        "metal_unit": "oz",
        "traps": ["inferred_nested_in_indicated"],
        "rows": [
            # category, Mt, grade, metal(oz)
            ("Measured", 4.10, 3.05, 402000.0),
            ("Indicated", 21.60, 2.740, 1903000.0),
            ("Inferred", 3.20, 2.150, 221000.0),   # 嵌套于 Indicated 内的增量
            ("Total", 28.90, 2.712, 2526000.0),
        ],
        "ground_truth": {
            "Indicated": {"tonnes_mt": 21.6, "grade": 2.74, "grade_unit": "g/t",
                          "metal": 1903000.0, "metal_unit": "oz",
                          "metal_tonnes": 1903000.0 * 31.1034768 / 1_000_000.0},
            "Inferred": {"tonnes_mt": 3.2, "grade": 2.15, "grade_unit": "g/t",
                         "metal": 221000.0, "metal_unit": "oz",
                         "metal_tonnes": 221000.0 * 31.1034768 / 1_000_000.0},
        },
    },
    {
        "id": "barrick",
        "file": "barrick_ni43-101_synthetic.pdf",
        "company": "Barrick-style Copper-Gold Inc. (SYNTHETIC)",
        "project": "Reko Diq Style Porphyry Cu-Au Project",
        "commodity": "Cu",
        "grade_unit": "%",
        "metal_unit": "Mlb",
        "traps": ["equivalent_grade_distractor", "Mlb_unit"],
        "rows": [
            ("Measured", 210.0, 0.48, 2220.0),      # 0.48 % Cu, 2220 Mlb Cu
            ("Indicated", 890.0, 0.41, 8040.0),
            ("Inferred", 150.0, 0.33, 1090.0),
            ("Total", 1250.0, 0.414, 11350.0),
        ],
        "ground_truth": {
            "Indicated": {"tonnes_mt": 890.0, "grade": 0.41, "grade_unit": "%",
                          "metal": 8040.0, "metal_unit": "Mlb",
                          "metal_tonnes": 8040.0 * 453.59237},
            "Inferred": {"tonnes_mt": 150.0, "grade": 0.33, "grade_unit": "%",
                         "metal": 1090.0, "metal_unit": "Mlb",
                         "metal_tonnes": 1090.0 * 453.59237},
        },
    },
    {
        "id": "pilbara",
        "file": "pilbara_ni43-101_synthetic.pdf",
        "company": "Pilbara-style Lithium Ltd. (SYNTHETIC)",
        "project": "Pilgangoora Style Spodumene Project, WA",
        "commodity": "Li2O",
        "grade_unit": "%",
        "metal_unit": "Mt",
        "traps": ["table_split_across_pages", "missing_grade_must_abstain"],
        "rows_indicated": [("Indicated", 108.0, 1.18, None)],
        "rows_inferred": [("Inferred", 42.5, None, None)],   # 品位缺失 → 应 abstain
        "ground_truth": {
            "Indicated": {"tonnes_mt": 108.0, "grade": 1.18, "grade_unit": "%",
                          "metal": None, "metal_unit": None, "metal_tonnes": None},
            "Inferred": {"tonnes_mt": 42.5, "grade": None, "grade_unit": None,
                         "metal": None, "metal_unit": None, "metal_tonnes": None,
                         "expect_abstain": True},
        },
    },
]


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=17, leading=21),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=12.5, leading=16),
        "body": ParagraphStyle("b", parent=base["BodyText"], fontSize=9.5, leading=13),
        "small": ParagraphStyle("s", parent=base["BodyText"], fontSize=8, leading=10.5,
                                textColor=colors.HexColor("#555555")),
    }


def _resource_table(rows: list[tuple], commodity: str, grade_unit: str,
                    metal_unit: str) -> Table:
    data = [["Category", "Tonnes (Mt)", f"Grade ({grade_unit})",
             f"Contained metal ({metal_unit})"]]
    for category, mt, grade, metal in rows:
        data.append([
            category,
            f"{mt:,.2f}",
            "—" if grade is None else f"{grade:.2f}",
            "—" if metal is None else f"{metal:,.0f}",
        ])
    table = Table(data, hAlign="LEFT", colWidths=[1.5 * inch, 1.2 * inch, 1.3 * inch, 1.9 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa5b1")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _front_matter(case: dict, s: dict) -> list:
    return [
        Paragraph("NI 43-101 Technical Report — SYNTHETIC SAMPLE", s["title"]),
        Spacer(1, 10),
        Paragraph(f"<b>Company:</b> {case['company']}", s["body"]),
        Paragraph(f"<b>Project:</b> {case['project']}", s["body"]),
        Paragraph("<b>Effective date:</b> December 31, 2024", s["body"]),
        Paragraph("<b>Report date:</b> February 14, 2025", s["body"]),
        Spacer(1, 8),
        Paragraph(
            "NOTICE: This document is a synthetic, machine-generated sample created solely for "
            "software testing of automated NI 43-101 data extraction. It contains no real "
            "mineral resource information and is not a disclosure for any issuer.", s["small"]),
        PageBreak(),
        Paragraph("1. Summary", s["h2"]),
        Paragraph(
            "This technical report has been prepared in accordance with National Instrument "
            "43-101 Standards of Disclosure for Mineral Projects. All mineral resource "
            "estimates are classified in accordance with CIM Definition Standards.", s["body"]),
        Spacer(1, 8),
        Paragraph("14. Mineral Resource Estimates", s["h2"]),
        Paragraph(
            "Mineral resources for the Project are reported at a cut-off grade and are "
            "classified as Measured, Indicated and Inferred. Mineral resources that are not "
            "mineral reserves do not have demonstrated economic viability.", s["body"]),
        Spacer(1, 6),
    ]


def _tail_matter(case: dict, s: dict) -> list:
    return [
        Spacer(1, 10),
        Paragraph(
            "Mineral Reserves are reported separately in Section 15 and are not included in "
            "the mineral resource tabulation above. Capital cost and economic analysis "
            "assumptions are detailed in Sections 21 and 22.", s["body"]),
        Spacer(1, 6),
        Paragraph(
            "Notes: (1) Tonnes are metric tonnes. (2) Contained metal is reported in "
            "accordance with the unit shown. (3) Totals may not sum exactly due to rounding. "
            "(4) Inferred mineral resources are considered too speculative geologically to "
            "have the economic considerations applied that would enable them to be "
            "categorized as mineral reserves.", s["small"]),
    ]


def build_pdf(case: dict, out_path: Path) -> None:
    s = _styles()
    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"NI 43-101 (SYNTHETIC) — {case['company']}",
        author="synthetic-fixture-generator",
    )
    story = _front_matter(case, s)

    if case["id"] == "pilbara":
        # 跨页陷阱：Indicated 表在第 1 页，Inferred 表头+数据在第 2 页
        story.append(_resource_table(case["rows_indicated"], case["commodity"],
                                     case["grade_unit"], case["metal_unit"]))
        story.append(Paragraph(
            "Table 14-2: Mineral Resources — Indicated (continued on following page)", s["small"]))
        story.append(PageBreak())
        story.append(Paragraph(
            "14.2 Mineral Resource Estimate (continued)", s["h2"]))
        story.append(_resource_table(case["rows_inferred"], case["commodity"],
                                     case["grade_unit"], case["metal_unit"]))
        story.append(Paragraph(
            "Table 14-3: Mineral Resources — Inferred (tonnage only; grade not estimated "
            "for this domain at the current drill density).", s["small"]))
    else:
        story.append(_resource_table(case["rows"], case["commodity"],
                                     case["grade_unit"], case["metal_unit"]))
        if case["id"] == "barrick":
            story.append(Paragraph(
                "Table 14-1: Mineral Resources at 0.20% Cu cut-off. Grade column shows % Cu; "
                "copper equivalent (CuEq) grades are reported in Table 14-4 below and are not "
                "the headline grade.", s["small"]))
            # 干扰表：CuEq 等价品位。抽错列就会把 0.63 当成 Cu 品位。
            story.append(Spacer(1, 12))
            story.append(_resource_table(
                [("Indicated", 890.0, 0.63, 8040.0), ("Inferred", 150.0, 0.51, 1090.0)],
                case["commodity"], "% CuEq", case["metal_unit"]))
            story.append(Paragraph(
                "Table 14-4: Copper equivalent grades (CuEq %), for comparison only.", s["small"]))
        else:
            story.append(Paragraph(
                "Table 14-1: Mineral Resources at 0.5 g/t Au cut-off. Inferred mineral "
                "resources shown on the Indicated line are additional to, and contained "
                "within, the Indicated mineral resource above.", s["small"]))

    story.extend(_tail_matter(case, s))
    doc.build(story)


def build_all(out_dir: Path | None = None, write_ground_truth: bool = True) -> dict:
    ensure_dirs()
    target = Path(out_dir or FIXTURE_DIR)
    target.mkdir(parents=True, exist_ok=True)

    manifest = {"generated": True, "note": "synthetic NI 43-101 samples for offline testing",
                "cases": []}
    ground_truth: dict[str, dict] = {}

    for case in CASES:
        pdf_path = target / case["file"]
        build_pdf(case, pdf_path)
        gt = {
            "case_id": case["id"],
            "file": case["file"],
            "company": case["company"],
            "project": case["project"],
            "commodity": case["commodity"],
            "grade_unit": case["grade_unit"],
            "metal_unit": case["metal_unit"],
            "traps": case["traps"],
            "resources": case["ground_truth"],
            "synthetic": True,
        }
        ground_truth[case["id"]] = gt
        manifest["cases"].append({
            "id": case["id"], "file": case["file"], "company": case["company"],
            "commodity": case["commodity"], "traps": case["traps"],
        })

    if write_ground_truth:
        (target / "ground_truth.json").write_text(
            json.dumps(ground_truth, ensure_ascii=False, indent=2), encoding="utf-8")
        (target / "sources.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"manifest": manifest, "ground_truth": ground_truth}


if __name__ == "__main__":
    result = build_all()
    for case in result["manifest"]["cases"]:
        print(f"generated: {case['file']}")
