"""NI 43-101 抽取质量测试：字段级 accuracy、陷阱处理与 abstain 行为。

对应题面「我们最看的是：当抽取明显错误时，系统是否 abstain 而不是硬给」。
三份合成样例各自植入一个陷阱，测试逐个验证系统没有被题目设计者骗到。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.mineral_pdf_mcp import extractor, pdf_text        # noqa: E402
from servers.mineral_pdf_mcp.server import (                   # noqa: E402
    evaluate, extract_resources_envelope,
)


def parse_fixture(fixtures_dir: Path, file_name: str) -> tuple[dict, list]:
    pages, warnings = pdf_text.extract_pages_from_file(str(fixtures_dir / file_name))
    return extractor.parse_resource_tables(pages), pages


def test_pdf_text_layer_is_extracted(fixtures_dir: Path) -> None:
    pages, _ = pdf_text.extract_pages_from_file(
        str(fixtures_dir / "newmont_ni43-101_synthetic.pdf"))
    text = "\n".join(page_text for _, page_text in pages)
    assert "Indicated" in text and "Inferred" in text
    assert "21.60" in text                      # 数值必须原样出现在文本层


@pytest.mark.parametrize("case_id", ["newmont", "barrick", "pilbara"])
def test_extraction_within_tolerance(fixtures_dir: Path, ground_truth: dict,
                                     case_id: str) -> None:
    """三份样例的 Indicated/Inferred 数值都必须落在 ±5% 容差内。"""
    case = ground_truth[case_id]
    result, _ = parse_fixture(fixtures_dir, case["file"])
    shaped = extractor.build_ground_truth_shaped(result)
    evaluation = evaluate(shaped, case)
    assert evaluation["wrongs"] == 0, evaluation["fields"]
    assert evaluation["accuracy"] is not None and evaluation["accuracy"] >= 0.8


def test_equivalent_grade_is_not_mistaken_for_headline_grade(fixtures_dir: Path,
                                                             ground_truth: dict) -> None:
    """Barrick 样例里 CuEq 干扰表紧跟主表；必须取 % Cu 主品位，而不是 0.63。"""
    case = ground_truth["barrick"]
    result, _ = parse_fixture(fixtures_dir, case["file"])
    shaped = extractor.build_ground_truth_shaped(result)
    assert shaped["Indicated"]["grade"] == pytest.approx(0.41, rel=0.05)
    assert shaped["Indicated"]["grade"] != pytest.approx(0.63, rel=0.01)


def test_nested_inferred_is_kept_and_flagged(fixtures_dir: Path,
                                             ground_truth: dict) -> None:
    """Newmont 样例里 Inferred 是以「含于 Indicated」的增量形式披露的。"""
    case = ground_truth["newmont"]
    result, _ = parse_fixture(fixtures_dir, case["file"])
    shaped = extractor.build_ground_truth_shaped(result)
    assert shaped["Inferred"]["tonnes_mt"] == pytest.approx(3.2, rel=0.05)
    assert shaped["Inferred"]["included_in"] == "Indicated"
    assert result["nested"], "嵌套关系必须被显式记录，供下游避免重复相加"


def test_total_row_excluded_from_categories(fixtures_dir: Path,
                                            ground_truth: dict) -> None:
    case = ground_truth["newmont"]
    result, _ = parse_fixture(fixtures_dir, case["file"])
    assert "Total" not in result["categories"]
    assert result["totals"], "合计行应单独保存，而不是丢弃"


def test_missing_grade_stays_null_and_triggers_abstain(fixtures_dir: Path,
                                                       ground_truth: dict) -> None:
    """Pilbara 样例的 Inferred 只有矿量、没有品位：必须是 null，不能补 0 或猜测。"""
    case = ground_truth["pilbara"]
    result, _ = parse_fixture(fixtures_dir, case["file"])
    shaped = extractor.build_ground_truth_shaped(result)
    assert shaped["Inferred"]["tonnes_mt"] == pytest.approx(42.5, rel=0.05)
    assert shaped["Inferred"]["grade"] is None
    assert shaped["Inferred"]["metal"] is None

    envelope = extract_resources_envelope(str(fixtures_dir / case["file"]))
    assert envelope.data["status"] in ("abstain", "partial")
    assert envelope.data["resources"]["Inferred"]["grade"] is None
    assert envelope.confidence < 1.0


def test_scanned_pdf_without_text_layer_abstains(tmp_path: Path) -> None:
    """没有文本层的 PDF 必须 abstain（提示需要 OCR），而不是返回空结果当 0。"""
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.pdfgen import canvas
    except ImportError:                                  # pragma: no cover
        pytest.skip("生成扫描件样张需要 reportlab（样例可选项，缺失时跳过本用例）")

    path = tmp_path / "scanned_like.pdf"
    pdf = canvas.Canvas(str(path), pagesize=LETTER)
    for page in range(3):
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, 720, " ")          # 故意不写任何可见文本
        pdf.showPage()
    pdf.save()

    envelope = extract_resources_envelope(str(path))
    assert envelope.data is None or envelope.data.get("status") == "abstain"
    assert envelope.error or any(
        warning["code"] == "abstain_insufficient_evidence"
        or warning["code"].startswith("low_text_density")
        for warning in envelope.to_dict()["warnings"]
    )


def test_grade_metal_cross_check_detects_inconsistency() -> None:
    """交叉校验：故意给出不自洽的金属量，必须被标记为不一致。"""
    row = extractor.ResourceRow(
        category="Indicated", tonnes_mt=100.0, grade_value=1.0, grade_unit="%",
        grade_is_equivalent=False, metal_value=500_000.0, metal_unit="t",
        metal_tonnes=500_000.0,
    )
    check = extractor.cross_check(row, tolerance_pct=5.0)
    assert check["consistent"] is False
    assert check["derived_tonnes"] == pytest.approx(1_000_000.0, rel=0.01)


def test_unknown_local_path_is_rejected() -> None:
    """越权本地路径必须被拒绝（只允许 workspace 内样例目录）。"""
    envelope = extract_resources_envelope("/etc/passwd")
    assert envelope.error and envelope.confidence == 0.0


# ---------------------------------------------------------------------------
# 真实 PDF 暴露出的两类问题（用合成数据复现，无需联网）
# ---------------------------------------------------------------------------

def test_content_stream_name_scan_uses_content_buffer() -> None:
    """内容流解析必须用内容流自己的 buffer 取字体名。

    真实 bug：早期版本在内容流循环里调用了 `parser._parse_name`，而它读的是
    `self.data`（整个 PDF 文件）。位置越界后从文件头乱取字节，实测取到
    'PDF-1.6' / 'ä'，导致 `/F5 1 Tf` 解析出错误的字体名 → current_font 恒为
    None → Type0/CID 文本全部退化成按字节硬解（乱码）。
    """
    from servers.mineral_pdf_mcp.pdf_text import _Parser, _scan_name

    # 内容流 buffer 与"文件 buffer"故意取不同内容，确保不再串用
    content = b"/F5 1 Tf (text) Tj"
    name, pos = _scan_name(content, 0)
    assert str(name) == "F5"
    assert content[pos:pos + 2] == b" 1"

    parser = _Parser(b"%PDF-1.6 something else entirely")
    # 旧实现若被误用：位置 0 处读的是 %PDF 之后的字节，会得到无关名字
    wrong, _ = parser._parse_name(0)
    assert str(wrong) != "F5"


def test_undecodable_text_layer_triggers_abstain() -> None:
    """文本层解出噪声时必须 abstain，且理由要明确，而不是含糊的"证据不足"。

    真实案例：某 SEC 技术报告的 printable=0.979、space=0.151，与正常报告
    的统计量几乎一致——**纯统计量区分不开**。因此最终判据是「领域词检查」：
    矿产报告解码成功必然出现 resource/grade/tonnes 等词，一个都没有即判坏。
    """
    from servers.mineral_pdf_mcp import pdf_text

    noise = ' rAp p$T"o68 rAp p$T"ti68 ' * 60              # 无任何领域词的噪声
    doc = pdf_text.PdfDocument(pages=[pdf_text.PdfPage(number=1, text=noise)])
    assert doc.text_layer_broken is True

    # 正常报告文本不应被误判
    real = ("Indicated mineral resources of 21.6 Mt at 2.74 g/t Au containing "
            "1,903,000 ounces. Inferred resources were estimated separately. ") * 12
    ok_doc = pdf_text.PdfDocument(pages=[pdf_text.PdfPage(number=1, text=real)])
    assert ok_doc.text_layer_broken is False

    # 短文本不下结论（统计量噪声太大）
    assert pdf_text.readability("Indicated resource 21.6 Mt")["verdict"] == "unknown"


def test_readability_flags_control_characters() -> None:
    """含控制字符/私用区 → 可靠地判为不可读（这是唯一可靠的统计判据）。"""
    from servers.mineral_pdf_mcp import pdf_text

    assert pdf_text.readability("")["verdict"] == "empty"
    assert pdf_text.readability("\x00\x01ëp@QðÀÀ\x0c\x08")["verdict"] == "unreadable"
    long_ok = ("Indicated mineral resources of 21.6 Mt at 2.74 g/t Au. " * 10)
    assert pdf_text.readability(long_ok)["verdict"] == "readable"


def test_envelope_marks_degradation_for_local_fixture(fixtures_dir: Path) -> None:
    envelope = extract_resources_envelope(
        str(fixtures_dir / "newmont_ni43-101_synthetic.pdf"))
    payload = envelope.to_dict()
    assert payload["provenance"]["sources"][0]["type"] == "local_file"
    assert payload["data"]["status"] in ("ok", "partial")
