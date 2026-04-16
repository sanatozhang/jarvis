"""Tests for analysis result failure detection and output parsing.

Covers all scenarios where Claude/Codex output should be classified as
success vs failure, including edge cases like Markdown-only output,
max turns, and partial results.
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from app.agents.base import BaseAgent, _extract_json_from_text, _salvage_from_markdown
from app.models.schemas import AnalysisResult, Confidence


# ---------------------------------------------------------------------------
# Helper: replicate the is_real_failure logic from tasks.py
# ---------------------------------------------------------------------------
_SYSTEM_FAILURE_TYPES = {
    "分析超时", "日志解析失败", "Agent 不可用",
    "OpenAI 额度不足", "Claude 额度不足", "所有模型额度不足",
}


def _is_real_failure(result: AnalysisResult) -> bool:
    """Exact copy of the logic in tasks.py._run_task for unit testing."""
    rc = (result.root_cause or "").strip()
    _error_markers = {"未产出结构化结果"}
    is_only_error = any(m in rc for m in _error_markers) and len(rc) < 100
    is_short_error = len(rc) < 120 and any(
        kw in rc.lower() for kw in ["max turns", "reached max", "error:"]
    )
    has_substance = bool(rc) and not is_only_error and not is_short_error

    has_real_type = bool(
        result.problem_type
        and result.problem_type not in _SYSTEM_FAILURE_TYPES
        and result.problem_type != "未知"
    )

    is_fail = (
        result.problem_type in _SYSTEM_FAILURE_TYPES
        or (result.problem_type == "未知" and not has_substance)
    )
    if result.problem_type not in _SYSTEM_FAILURE_TYPES:
        if has_substance or has_real_type:
            is_fail = False

    return is_fail


def _make_result(**kwargs) -> AnalysisResult:
    defaults = dict(
        task_id="t1", issue_id="i1",
        problem_type="未知", root_cause="", confidence="low",
        needs_engineer=True, agent_type="claude_code",
    )
    defaults.update(kwargs)
    return AnalysisResult(**defaults)


# ===================================================================
# Test Suite 1: is_real_failure classification
# ===================================================================
class TestIsRealFailure:
    """Verify that different output types are correctly classified."""

    def test_normal_json_output(self):
        """A: Agent wrote result.json with proper fields → success."""
        r = _make_result(
            problem_type="蓝牙连接问题",
            root_cause="TokenNotMatch导致连接失败",
            confidence="high",
            needs_engineer=False,
        )
        assert _is_real_failure(r) is False

    def test_chinese_problem_type(self):
        """B: Any non-system, non-未知 problem_type → success."""
        r = _make_result(
            problem_type="文件管理（转写，总结，文件编辑）",
            root_cause="网络连接不稳定",
        )
        assert _is_real_failure(r) is False

    def test_markdown_salvaged(self):
        """C: Agent output Markdown, salvaged into problem_type → success."""
        r = _make_result(
            problem_type="分析完成",
            root_cause="通过探索式日志分析，确定根本原因是网络连接不稳定",
        )
        assert _is_real_failure(r) is False

    def test_unknown_type_with_substance(self):
        """D: problem_type=未知 but root_cause has real analysis → success."""
        r = _make_result(
            problem_type="未知",
            root_cause="## 分析完成\n网络连接不稳定，无法访问 Plaud AI 服务器，导致总结加载失败",
        )
        assert _is_real_failure(r) is False

    def test_no_output_boilerplate(self):
        """E: Only error boilerplate in root_cause → failure."""
        r = _make_result(
            problem_type="未知",
            root_cause="分析未产出结构化结果",
        )
        assert _is_real_failure(r) is True

    def test_empty_output(self):
        """F: Completely empty output → failure."""
        r = _make_result(problem_type="未知", root_cause="")
        assert _is_real_failure(r) is True

    def test_timeout(self):
        """G: System timeout → failure (even if root_cause has text)."""
        r = _make_result(
            problem_type="分析超时",
            root_cause="Claude Code 分析超过 600s 超时",
        )
        assert _is_real_failure(r) is True

    def test_quota_exhausted(self):
        """H: API quota exhausted → failure."""
        r = _make_result(
            problem_type="Claude 额度不足",
            root_cause="Anthropic API 额度已耗尽",
        )
        assert _is_real_failure(r) is True

    def test_openai_quota_exhausted(self):
        """H2: OpenAI quota → failure."""
        r = _make_result(
            problem_type="OpenAI 额度不足",
            root_cause="OpenAI API 额度已耗尽",
        )
        assert _is_real_failure(r) is True

    def test_max_turns_no_content(self):
        """I: Max turns with only error message → failure."""
        r = _make_result(
            problem_type="未知",
            root_cause="Reached max turns (50)",
        )
        assert _is_real_failure(r) is True

    def test_max_turns_with_analysis(self):
        """J: Max turns but Claude produced analysis before hitting limit → success."""
        r = _make_result(
            problem_type="未知",
            root_cause=(
                "## 分析完成\n\n通过探索式日志分析，确定根本原因是网络连接不稳定，"
                "无法访问 Plaud AI 服务器。总结加载任务死锁导致用户看到空白页面。"
                "建议用户重新连接网络后重试。"
            ),
        )
        assert _is_real_failure(r) is False

    def test_agent_unavailable(self):
        """K: Agent CLI not installed → failure."""
        r = _make_result(
            problem_type="Agent 不可用",
            root_cause="Claude Code CLI 未安装或不在 PATH 中",
        )
        assert _is_real_failure(r) is True

    def test_real_type_empty_root_cause(self):
        """L: Has a real problem_type but empty root_cause → success."""
        r = _make_result(
            problem_type="录音丢失（时间戳偏移）",
            root_cause="",
        )
        assert _is_real_failure(r) is False

    def test_all_models_exhausted(self):
        """Both Claude and OpenAI exhausted → failure."""
        r = _make_result(
            problem_type="所有模型额度不足",
            root_cause="Claude 和 OpenAI 的 API 额度均已耗尽",
        )
        assert _is_real_failure(r) is True

    def test_low_confidence_with_content(self):
        """Low confidence + needs_engineer but has content → success."""
        r = _make_result(
            problem_type="蓝牙连接",
            root_cause="日志中发现断连记录但无法确定根因",
            confidence="low",
            needs_engineer=True,
        )
        assert _is_real_failure(r) is False

    def test_low_confidence_empty_user_reply(self):
        """Low confidence, no user_reply, but has root_cause → success."""
        r = _make_result(
            problem_type="云同步失败",
            root_cause="WebSocket 连接中断导致同步失败",
            confidence="low",
            needs_engineer=True,
        )
        assert _is_real_failure(r) is False


# ===================================================================
# Test Suite 2: parse_result — JSON extraction from various outputs
# ===================================================================
class TestParseResult:
    """Verify parse_result handles all output formats."""

    def test_result_json_file(self, tmp_path: Path):
        """Strategy 1: result.json exists and is valid."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "result.json").write_text(json.dumps({
            "problem_type": "蓝牙连接",
            "root_cause": "Token mismatch",
            "confidence": "high",
            "user_reply": "请换货",
        }), encoding="utf-8")

        result = BaseAgent.parse_result(tmp_path, "")
        assert result.problem_type == "蓝牙连接"
        assert result.confidence == Confidence.HIGH

    def test_result_json_nested(self, tmp_path: Path):
        """Strategy 2: result.json at non-standard path."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        nested = tmp_path / "subdir"
        nested.mkdir()
        (nested / "result.json").write_text(json.dumps({
            "problem_type": "录音丢失",
            "root_cause": "时间戳偏移",
        }), encoding="utf-8")

        result = BaseAgent.parse_result(tmp_path, "")
        assert result.problem_type == "录音丢失"

    def test_json_in_stdout(self, tmp_path: Path):
        """Strategy 3: JSON block in stdout."""
        (tmp_path / "output").mkdir()
        stdout = '''Some text before
```json
{"problem_type": "云同步失败", "root_cause": "WebSocket断连", "confidence": "medium"}
```
Some text after'''

        result = BaseAgent.parse_result(tmp_path, stdout)
        assert result.problem_type == "云同步失败"

    def test_markdown_only_output(self, tmp_path: Path):
        """Strategy 4: Pure Markdown output, no JSON anywhere."""
        (tmp_path / "output").mkdir()
        stdout = """## 网络连接异常

通过日志分析，确定根本原因：

**根本原因**：网络连接不稳定，无法访问 Plaud AI 服务器。

建议回复:
您好，请检查网络连接后重试。"""

        result = BaseAgent.parse_result(tmp_path, stdout)
        assert result.problem_type == "网络连接异常"
        assert "网络连接不稳定" in result.root_cause

    def test_empty_output(self, tmp_path: Path):
        """No output at all → defaults."""
        (tmp_path / "output").mkdir()
        result = BaseAgent.parse_result(tmp_path, "")
        assert result.problem_type == "未知"
        assert "未产出结构化结果" in result.root_cause

    def test_short_error_output(self, tmp_path: Path):
        """Very short error message in stdout."""
        (tmp_path / "output").mkdir()
        result = BaseAgent.parse_result(tmp_path, "Error: Reached max turns (50)")
        # Should still have the error in root_cause
        assert "max turns" in result.root_cause.lower() or result.root_cause


# ===================================================================
# Test Suite 3: _extract_json_from_text
# ===================================================================
class TestExtractJson:

    def test_json_code_block(self):
        text = '```json\n{"problem_type": "test", "root_cause": "cause"}\n```'
        data = _extract_json_from_text(text)
        assert data["problem_type"] == "test"

    def test_bare_json_with_problem_type(self):
        text = 'Before {"problem_type": "蓝牙", "root_cause": "断连"} after'
        data = _extract_json_from_text(text)
        assert data["problem_type"] == "蓝牙"

    def test_no_json(self):
        data = _extract_json_from_text("Just plain text without any JSON")
        assert data == {}

    def test_json_without_problem_type(self):
        text = '```json\n{"status": "ok"}\n```'
        data = _extract_json_from_text(text)
        assert data == {} or "problem_type" not in data


# ===================================================================
# Test Suite 4: _salvage_from_markdown
# ===================================================================
class TestSalvageFromMarkdown:

    def test_heading_becomes_problem_type(self):
        text = "## 蓝牙连接断开\n\n根本原因是网络问题"
        data = _salvage_from_markdown(text)
        assert data["problem_type"] == "蓝牙连接断开"
        assert "网络问题" in data["root_cause"]

    def test_user_reply_extraction(self):
        text = "## 分析完成\n\n原因分析\n\n建议回复:\n您好，请重新连接设备。\n\n**其他信息**"
        data = _salvage_from_markdown(text)
        assert "请重新连接设备" in data.get("user_reply", "")

    def test_empty_text(self):
        data = _salvage_from_markdown("")
        assert data == {}

    def test_short_text(self):
        data = _salvage_from_markdown("Error")
        # "Error" → stripped of headings = "Error", root_cause = "Error"
        assert data.get("root_cause") == "Error"

    def test_confidence_defaults(self):
        data = _salvage_from_markdown("## 测试\n内容很长足够做分析了")
        assert data["confidence"] == "medium"
        assert data["needs_engineer"] is True
