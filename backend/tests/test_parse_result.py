"""Tests for BaseAgent.parse_result — especially fallback/salvage scenarios."""

import json
from pathlib import Path

from app.agents.base import BaseAgent, _safe_confidence, _safe_key_evidence, _extract_json_from_text, _salvage_from_markdown
from app.models.schemas import Confidence


# ---------------------------------------------------------------------------
# _safe_confidence: tolerates case variations & Chinese
# ---------------------------------------------------------------------------

def test_safe_confidence_case_insensitive():
    assert _safe_confidence("High") == Confidence.HIGH
    assert _safe_confidence("HIGH") == Confidence.HIGH
    assert _safe_confidence("Medium") == Confidence.MEDIUM
    assert _safe_confidence("LOW") == Confidence.LOW


def test_safe_confidence_chinese():
    assert _safe_confidence("高") == Confidence.HIGH
    assert _safe_confidence("中等") == Confidence.MEDIUM


def test_safe_confidence_fallback():
    assert _safe_confidence("unknown_value") == Confidence.LOW
    assert _safe_confidence("") == Confidence.LOW
    assert _safe_confidence(None) == Confidence.LOW


# ---------------------------------------------------------------------------
# _safe_key_evidence: handles string or list
# ---------------------------------------------------------------------------

def test_safe_key_evidence_list():
    assert _safe_key_evidence(["line1", "line2"]) == ["line1", "line2"]


def test_safe_key_evidence_string():
    result = _safe_key_evidence("line1\nline2\nline3")
    assert result == ["line1", "line2", "line3"]


def test_safe_key_evidence_non_list():
    assert _safe_key_evidence(42) == []
    assert _safe_key_evidence(None) == []


# ---------------------------------------------------------------------------
# _extract_json_from_text: tolerates trailing commas and multiple code blocks
# ---------------------------------------------------------------------------

def test_extract_json_trailing_comma():
    text = '```json\n{"problem_type": "蓝牙异常", "root_cause": "连接断开",}\n```'
    result = _extract_json_from_text(text)
    assert result["problem_type"] == "蓝牙异常"


def test_extract_json_no_code_fence():
    text = 'Some text before {"problem_type": "固件失败", "root_cause": "下载中断"} and after'
    result = _extract_json_from_text(text)
    assert result["problem_type"] == "固件失败"


def test_extract_json_empty():
    assert _extract_json_from_text("no json here") == {}


# ---------------------------------------------------------------------------
# _salvage_from_markdown: extracts from conversational model output
# ---------------------------------------------------------------------------

def test_salvage_from_markdown_with_bold_labels():
    text = """根据日志分析：

**问题类型**: 固件升级失败

**根本原因**: 设备在固件下载过程中连接断开，导致安装包不完整。

**建议回复**:
您好，经过排查，设备固件升级失败的原因是下载过程中连接中断。请保持设备靠近手机重新尝试升级。
"""
    result = _salvage_from_markdown(text)
    assert "固件升级失败" in result.get("problem_type", "")
    assert len(result.get("root_cause", "")) > 20
    assert result.get("user_reply")


def test_salvage_from_markdown_heading_style():
    text = """### 蓝牙连接异常

设备蓝牙在配对过程中频繁断开，日志显示 GATT 连接超时。

### 用户回复:
您好，您的设备蓝牙连接不稳定，建议重启设备后重新配对。
"""
    result = _salvage_from_markdown(text)
    assert "蓝牙" in result.get("problem_type", "")
    assert "GATT" in result.get("root_cause", "")


def test_salvage_from_markdown_empty():
    assert _salvage_from_markdown("") == {}
    # Very short strings still get salvaged (caller checks len > 50 before invoking)
    result = _salvage_from_markdown("hi")
    assert result.get("root_cause") == "hi"


# ---------------------------------------------------------------------------
# parse_result: full integration — model outputs text but no result.json
# ---------------------------------------------------------------------------

def test_parse_result_no_json_uses_raw_output(tmp_path: Path):
    """When result.json doesn't exist and raw_output has analysis content,
    the result should still have meaningful root_cause (not '分析未产出结构化结果')."""
    (tmp_path / "output").mkdir()
    raw = """根据日志分析，该设备固件升级失败的原因是：

1. 固件下载成功完成（100%）
2. 但安装阶段设备连接断开
3. 导致安装进程无法启动

建议用户重新尝试升级，保持设备靠近手机。"""

    result = BaseAgent.parse_result(tmp_path, raw)
    assert result.root_cause
    assert "分析未产出结构化结果" not in result.root_cause
    assert "固件" in result.root_cause or "升级" in result.root_cause


def test_parse_result_valid_json(tmp_path: Path):
    """Normal case: result.json exists and is valid."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    data = {
        "problem_type": "蓝牙异常",
        "root_cause": "GATT 连接超时导致配对失败",
        "confidence": "high",
        "key_evidence": ["BLE disconnect at 10:23:45"],
        "user_reply": "您好，请重启设备后重新配对。",
        "needs_engineer": False,
    }
    (output_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False))

    result = BaseAgent.parse_result(tmp_path, "")
    assert result.problem_type == "蓝牙异常"
    assert result.confidence == Confidence.HIGH
    assert result.needs_engineer is False


def test_parse_result_confidence_case_tolerance(tmp_path: Path):
    """Model writes confidence as 'High' instead of 'high' — should not crash."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    data = {
        "problem_type": "录音丢失",
        "root_cause": "文件传输中断",
        "confidence": "High",
        "user_reply": "请重新同步。",
    }
    (output_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False))

    result = BaseAgent.parse_result(tmp_path, "")
    assert result.confidence == Confidence.HIGH
    assert result.problem_type == "录音丢失"


def test_parse_result_problem_type_cleaned(tmp_path: Path):
    """Model writes '分析完成' as problem_type — should be cleaned to '未知'."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    data = {
        "problem_type": "分析完成",
        "root_cause": "蓝牙配对过程中 GATT 超时",
        "confidence": "medium",
    }
    (output_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False))

    result = BaseAgent.parse_result(tmp_path, "")
    assert result.problem_type == "未知"
    assert "GATT" in result.root_cause
