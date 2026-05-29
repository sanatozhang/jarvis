"""回归测试：5 条 inaccurate 工单暴露的 4 类准确率 bug 的修复。

对应 2026-05-29 排查的 fb_f86c656539 / fb_fb4107609a / fb_9f347bbc90 /
fb_df8889bcff / fb_48030779f7，分别命中：
  ① 日志时段不覆盖问题 → 应短路出"需用户重传"，不硬跑 agent
  ② system_failure 半成品 / 假高置信 → 不得当"已完成可信"发布、置信钳到 low
  ③ markdown 兜底解析把章节标题 "Root Cause" 当成 problem_type
  ④ followup 追问的"客服处理方案"叙述污染了 root_cause（技术根因丢失）
"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.base import BaseAgent, _clean_problem_type, _salvage_from_markdown, _SECTION_HEADINGS
from app.models.schemas import Confidence
from app.workers.analysis_worker import (
    _build_stale_log_result,
    _check_log_coverage,
    _looks_like_followup_narrative,
    _parse_problem_ref_time,
    _sanitize_followup_result,
)


# ===================== ③ 章节标题不再被当 problem_type =====================

def test_salvage_skips_section_heading_root_cause():
    """fb_df8889bcff：'## Root Cause' 不得变成 problem_type。"""
    md = """## Root Cause

The user's in-app storage clear worked correctly, but iOS still shows >1GB because
the app stores large system files (a speaker_embedding ONNX model) in Documents/ that
are not touched by the in-app clear. That is the real reason storage stays high.

## User Reply
Please clear the cache manually under settings.
"""
    r = _salvage_from_markdown(md)
    assert (r.get("problem_type", "") or "").strip().lower() not in _SECTION_HEADINGS
    assert (r.get("problem_type", "") or "").strip().lower() != "root cause"
    # 但 root_cause 本体仍应被提取出来
    assert len(r.get("root_cause", "")) > 30


def test_clean_problem_type_rejects_completion_phrase():
    """rec27apb5yapZ：'追问分析完成' 是过程状态，不是问题分类 → 应判'未知'。"""
    assert _clean_problem_type("追问分析完成") == "未知"
    assert _clean_problem_type("分析完成") == "未知"
    assert _clean_problem_type("Analysis Complete") == "未知"
    # section 标题也走这条统一兜底
    assert _clean_problem_type("Root Cause") == "未知"
    # 真分类不受影响
    assert _clean_problem_type("蓝牙连接异常") == "蓝牙连接异常"


def test_salvage_keeps_real_problem_heading():
    """真正的问题分类标题仍应被采纳为 problem_type。"""
    md = """## 蓝牙连接频繁断开

设备在录音过程中每隔几分钟与 APP 断连一次，日志显示 BLE 链路被系统回收。
"""
    r = _salvage_from_markdown(md)
    assert "蓝牙" in r.get("problem_type", "")


# ===================== ② system_failure 权威化 + 置信钳位 =====================

def _write_result_json(tmp_path: Path, payload: dict) -> Path:
    ws = tmp_path / "ws"
    (ws / "output").mkdir(parents=True)
    (ws / "output" / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return ws


def test_system_failure_clamps_confidence_to_low(tmp_path):
    """fb_9f347bbc90：system_failure=True 却 confidence=high → 必须钳到 low。"""
    ws = _write_result_json(tmp_path, {
        "problem_type": "firmware_ota_user_skipped",
        "problem_type_en": "firmware_ota_user_skipped",
        "root_cause": "用户长期点击'知道了'跳过升级，OTA 流程本身无错误。" * 2,
        "confidence": "high",
        "system_failure": True,
    })
    result = BaseAgent.parse_result(ws, raw_output="")
    assert result.system_failure is True
    assert result.confidence == Confidence.LOW


def test_signature_ending_user_reply_not_truncation(tmp_path):
    """fb_dbe8f0f110：root_cause 完整(句号收尾)，user_reply 是以签名结尾的完整邮件
    （'Plaud Support Team'），不得被误判截断 → system_failure 必须 False。"""
    full_reply = (
        "Dear User,\n\nThank you for reaching out. Based on the logs, your device is not "
        "broadcasting a BLE signal. Please try a hard reset; if it persists, contact us for "
        "inspection or replacement.\n\nWe are here to help.\n\nBest regards,\nPlaud Support Team"
    )
    ws = _write_result_json(tmp_path, {
        "problem_type": "蓝牙连接异常 - 搜索不到设备",
        "root_cause": "日志显示设备从未出现在任何 BLE 扫描结果中，最可能是设备未在广播 BLE 信号。",  # 句号收尾
        "confidence": "low",
        "user_reply": full_reply,
        "user_reply_en": full_reply,
    })
    result = BaseAgent.parse_result(ws, raw_output="")
    assert result.system_failure is False  # 关键：签名结尾不算截断


def test_truncated_root_cause_flags_system_failure(tmp_path):
    """fb_fb4107609a：root_cause 半句截断 → 标 system_failure 且置信 low。"""
    ws = _write_result_json(tmp_path, {
        "problem_type": "说话人标注变灰",
        "root_cause": "用户反映说话人标注功能变灰，日志分析揭示关键信息：现有日志仅覆盖设备初始激活当天，而用户描述的问题发生在",  # 无结束标点 = 截断
        "confidence": "medium",
    })
    result = BaseAgent.parse_result(ws, raw_output="")
    assert result.system_failure is True
    assert result.confidence == Confidence.LOW


# ===================== ④ followup 污染护栏 =====================

def test_followup_narrative_detected():
    polluted = (
        "---\n\n**分析完成。** 以下是针对追问「怎么帮用户处理问题」的核心结论：\n\n"
        "客服处理方案总结\n结论：录音无法恢复"
    )
    assert _looks_like_followup_narrative(polluted) is True


def test_followup_narrative_not_false_positive():
    clean_rc = "BLE 传输确认后设备立即删除文件，APP 又被来电终止未落盘，导致录音永久丢失。"
    assert _looks_like_followup_narrative(clean_rc) is False


def test_sanitize_restores_technical_root_cause():
    """fb_48030779f7：root_cause 被追问叙述污染 → 恢复上次技术根因。"""
    polluted = "---\n\n以下是针对追问的核心结论：\n客服处理方案总结\n结论：录音无法恢复"
    result = SimpleNamespace(root_cause=polluted, user_reply="", problem_type="客服处理方案总结")
    prev = {
        "root_cause": "设备在 stopSyncFile 后立即删除文件，未等 APP 确认落盘，APP 又被来电强制终止，录音从未写入本地磁盘。",
        "problem_type": "录音丢失",
    }
    out = _sanitize_followup_result(result, prev, "fb_test")
    assert out.root_cause == prev["root_cause"]
    assert out.problem_type == "录音丢失"
    # 追问叙述不丢，挪进 user_reply
    assert "客服处理方案" in out.user_reply


def test_sanitize_noop_without_previous():
    polluted = "---\n以下是针对追问的核心结论"
    result = SimpleNamespace(root_cause=polluted, user_reply="", problem_type="x")
    out = _sanitize_followup_result(result, None, "fb_test")
    assert out.root_cause == polluted  # 没有上次结果可恢复 → 原样不动


def test_sanitize_noop_when_root_cause_clean():
    clean = "设备固件 bug：同步未完成即删除文件。"
    result = SimpleNamespace(root_cause=clean, user_reply="reply", problem_type="录音丢失")
    out = _sanitize_followup_result(result, {"root_cause": "x" * 50, "problem_type": "y"}, "fb_test")
    assert out.root_cause == clean  # 未污染 → 不动
    assert out.problem_type == "录音丢失"


# ===================== ① 日志时效性预检 =====================

@pytest.fixture
def issue_may28():
    return SimpleNamespace(
        id="fb_test", occurred_at=None, created_at=datetime(2026, 5, 28), description=""
    )


def _write_log(tmp_path: Path, name: str, day: str) -> Path:
    p = tmp_path / name
    p.write_text(
        "\n".join(f"{day} 1{i % 9}:00:0{i % 9} INFO line {i}" for i in range(60)),
        encoding="utf-8",
    )
    return p


def test_coverage_flags_stale_log(tmp_path, issue_may28):
    """fb_f86c656539：日志只到 1/30，问题在 5/28 → STALE。"""
    old_log = _write_log(tmp_path, "old.log", "2026-01-30")
    cov = _check_log_coverage([old_log], None, issue_may28, max_gap_days=30)
    assert cov is not None
    assert cov["gap_days"] > 30


def test_coverage_passes_fresh_log(tmp_path, issue_may28):
    new_log = _write_log(tmp_path, "new.log", "2026-05-27")
    assert _check_log_coverage([new_log], None, issue_may28, max_gap_days=30) is None


def test_coverage_conservative_on_no_timestamps(tmp_path, issue_may28):
    """无可解析时间戳 → 不敢妄断，放行（保守）。"""
    p = tmp_path / "nots.log"
    p.write_text("hello world\nno timestamp here\n", encoding="utf-8")
    assert _check_log_coverage([p], None, issue_may28, max_gap_days=30) is None


def test_coverage_no_reference_time_passes(tmp_path):
    """既无 problem_date 也无 occurred_at/created_at → 无法判定，放行。"""
    issue = SimpleNamespace(id="x", occurred_at=None, created_at=None, description="")
    old_log = _write_log(tmp_path, "old.log", "2026-01-30")
    assert _check_log_coverage([old_log], None, issue, max_gap_days=30) is None


def test_parse_problem_ref_time_formats(issue_may28):
    assert _parse_problem_ref_time("2026-05-28", issue_may28) == datetime(2026, 5, 28)
    assert _parse_problem_ref_time("2026-05-28 14:30:00", issue_may28) == datetime(2026, 5, 28, 14, 30, 0)
    # problem_date 解析失败 → 回退 issue.created_at
    assert _parse_problem_ref_time("garbage", issue_may28) == datetime(2026, 5, 28)


def test_stale_result_routes_to_user_retry(issue_may28):
    """短路结果应是 needs_user_retry，且 problem_type 不落入 system_failure 桶。"""
    cov = {
        "ref": datetime(2026, 5, 28),
        "last_event": datetime(2026, 1, 30),
        "first_event": datetime(2026, 1, 30),
        "gap_days": 118,
    }
    r = _build_stale_log_result(issue_may28, "task_x", cov, {})
    assert r.needs_user_retry is True
    assert r.system_failure is False
    assert r.confidence == Confidence.LOW
    assert "上传" in r.user_reply and "upload" in r.user_reply_en.lower()
