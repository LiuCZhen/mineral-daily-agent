"""LLM 路径的回归测试：交叉校验、JSON 容错解析、不可用时的显式降级。

这些用例不访问外网：LLM 调用被 monkeypatch 成桩函数，验证的是**编排与判定逻辑**，
而不是模型本身的质量（那是评测集的工作）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common import llm as llm_module                 # noqa: E402
from servers.common.llm import LLMClient, LLMUnavailable, extract_json_object  # noqa: E402
from servers.mineral_pdf_mcp import server as pdf_server      # noqa: E402


def test_extract_json_object_handles_fences_and_prose() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('Sure! {"a": {"b": 2}} hope this helps') == {"a": {"b": 2}}
    assert extract_json_object("no json here") is None
    assert extract_json_object("") is None


def test_llm_client_requires_api_key() -> None:
    """未配置 key 时必须显式抛 LLMUnavailable，而不是静默返回空结果。"""
    with pytest.raises(LLMUnavailable):
        LLMClient(role="extractor", api_key="")


def test_cross_validate_detects_grade_conflict() -> None:
    deterministic = {"Indicated": {"tonnes_mt": 890.0, "grade": 0.41, "metal": 8040.0}}
    llm_result = {"Indicated": {"tonnes_mt": 890.0, "grade": 0.63, "metal": 8040.0}}
    conflicts = pdf_server._cross_validate(deterministic, llm_result, 5.0)  # noqa: SLF001
    assert len(conflicts) == 1
    assert "Indicated.grade" in conflicts[0]
    assert "53.7%" in conflicts[0] or "53.6%" in conflicts[0]


def test_cross_validate_ignores_within_tolerance_and_missing() -> None:
    deterministic = {"Indicated": {"tonnes_mt": 890.0, "grade": 0.41, "metal": 8040.0}}
    within = {"Indicated": {"tonnes_mt": 888.0, "grade": 0.415, "metal": None}}
    assert pdf_server._cross_validate(deterministic, within, 5.0) == []   # noqa: SLF001
    assert pdf_server._cross_validate(deterministic, None, 5.0) == []     # noqa: SLF001


def test_llm_conflict_lowers_confidence_and_is_reported(fixtures_dir: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """模型给出与确定性解析冲突的品位时：保留确定性结果 + 告警 + 降置信度。"""
    monkeypatch.setattr(pdf_server, "_llm_shaped", lambda pages: (
        {"Indicated": {"tonnes_mt": 890.0, "grade": 0.63, "grade_unit": "%",
                       "metal": 8040.0, "metal_unit": "Mlb"}}, None))

    envelope = pdf_server.extract_resources_envelope(
        str(fixtures_dir / "barrick_ni43-101_synthetic.pdf"))
    payload = envelope.to_dict()
    codes = {warning["code"] for warning in payload["warnings"]}

    assert "cross_path_conflict" in codes
    assert payload["provenance"]["confidence"] < 0.95
    # 关键：以确定性解析器为准，模型不能覆盖
    assert payload["data"]["resources"]["Indicated"]["grade"] == pytest.approx(0.41, rel=0.05)


def test_llm_unavailable_note_is_informational(fixtures_dir: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_server, "_llm_shaped",
                        lambda pages: (None, "llm_not_configured"))
    envelope = pdf_server.extract_resources_envelope(
        str(fixtures_dir / "newmont_ni43-101_synthetic.pdf"))
    payload = envelope.to_dict()
    info = [warning for warning in payload["warnings"]
            if warning["code"] == "llm_path_skipped"]
    assert info and info[0]["severity"] == "info"
    assert payload["data"]["cross_validation"]["llm_available"] is False


def test_complete_json_raises_when_model_returns_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LLMClient(role="extractor", api_key="stub")
    monkeypatch.setattr(client, "complete",
                        lambda *a, **k: llm_module.LLMResponse(
                            text="I cannot help with that.", model="stub", usage={}, raw={}))
    with pytest.raises(LLMUnavailable):
        client.complete_json("extract")


def test_complete_json_parses_fenced_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LLMClient(role="critic", api_key="stub")
    monkeypatch.setattr(client, "complete",
                        lambda *a, **k: llm_module.LLMResponse(
                            text='```json\n{"score": 8, "issues": []}\n```',
                            model="stub", usage={}, raw={}))
    assert client.complete_json("score this") == {"score": 8, "issues": []}
