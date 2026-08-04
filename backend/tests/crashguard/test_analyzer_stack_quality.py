"""批次 5：prompt 主堆栈取值顺序 + 栈质量信号注入 analyzer.py 的单测。

覆盖：
1. 回归测试（最核心）：_run_in_background 里 snapshot_data["stack_trace"] 必须是
   _build_enrichment_block 回写之后的新值，不能是 _issue_to_dict 阶段取的旧值。
2. _stack_quality_note 文案 + _build_prompt 输出里包含对应质量提示。
3. _build_prompt 在没有显式传 stack_quality_note 时不抛 KeyError（setdefault 兜底）。
4. _FOLLOWUP_PROMPT_TEMPLATE / _build_followup_prompt 未受影响。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


OLD_STACK = "OLD_UNSYMBOLICATED_MARKER\n0x1a2b3c abs 00001234 _kDartIsolateSnapshotInstructions"
NEW_STACK = "NEW_SYMBOLICATED_MARKER\npackage:plaud_flutter_common/foo.dart:42 someMethod"


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


async def _seed(factory, *, issue_id: str, run_id: str, stack_trace: str):
    from app.crashguard.models import CrashIssue, CrashAnalysis
    async with factory() as s:
        s.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="flutter",
            title="test issue",
            representative_stack=stack_trace,
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        ))
        s.add(CrashAnalysis(
            datadog_issue_id=issue_id,
            analysis_run_id=run_id,
            status="pending",
            followup_question="",
            root_cause="",
            created_at=datetime.utcnow(),
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_run_in_background_uses_fresh_stack_after_enrichment(patched_session, tmp_path):
    """核心回归测试：_build_enrichment_block 回写新堆栈后，prompt 里必须是新值，不是旧值。

    如果 stack_trace 的取值顺序又被改回"先取旧值、之后不重读"，这个测试要能失败。
    """
    from app.crashguard.services.analyzer import _run_in_background, AnalysisOutput
    from app.crashguard.models import CrashIssue

    issue_id = "iss-stack-order"
    run_id = "run-stack-order"
    await _seed(patched_session, issue_id=issue_id, run_id=run_id, stack_trace=OLD_STACK)

    captured_snapshot = {}

    def _fake_build_prompt(snapshot_data):
        captured_snapshot.update(snapshot_data)
        return "FAKE_PROMPT"

    async def _fake_enrichment_block(issue_id_arg):
        # 模拟 _build_enrichment_block 内部重新符号化并把结果回写进 DB
        # （现实里是 _persist_distribution_to_issue 做的）。
        async with patched_session() as s:
            row = (await s.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id_arg)
            )).scalar_one()
            row.representative_stack = NEW_STACK
            await s.commit()
        return ""

    fake_output = AnalysisOutput(
        scenario="", root_cause="", fix_suggestion="", feasibility_score=0.0,
        confidence="low", reproducibility="unknown", raw_output="", agent_name="test-agent",
    )

    with patch(
        "app.crashguard.services.analyzer._build_enrichment_block",
        new=AsyncMock(side_effect=_fake_enrichment_block),
    ), patch(
        "app.crashguard.services.analyzer._prepare_workspace",
        return_value=tmp_path,
    ), patch(
        "app.crashguard.services.analyzer._build_prompt",
        side_effect=_fake_build_prompt,
    ), patch(
        "app.crashguard.services.analyzer._run_agent",
        new=AsyncMock(return_value=fake_output),
    ):
        await _run_in_background(issue_id, run_id)

    assert captured_snapshot, "「_build_prompt」应该被调用过一次"
    assert "NEW_SYMBOLICATED_MARKER" in captured_snapshot["stack_trace"]
    assert "OLD_UNSYMBOLICATED_MARKER" not in captured_snapshot["stack_trace"]

    # 质量标签也应该基于新堆栈算出来（NEW_STACK 含 .dart: → symbolicated_dart）
    from app.crashguard.services.analyzer import _stack_quality_note
    assert captured_snapshot["stack_quality_note"] == _stack_quality_note("symbolicated_dart")


@pytest.mark.parametrize(
    "quality, expected_fragment",
    [
        ("raw", "未能符号化"),
        ("symbolicated_native", "iOS 符号"),
        ("symbolicated_dart", "Dart 文件路径"),
        ("aot_pointers_unsymbolicated", "flutter symbolize"),
        ("empty", "无堆栈内容"),
    ],
)
def test_stack_quality_note_text(quality, expected_fragment):
    from app.crashguard.services.analyzer import _stack_quality_note
    assert expected_fragment in _stack_quality_note(quality)


def test_build_prompt_includes_stack_quality_note_fragment():
    from app.crashguard.services.analyzer import _build_prompt, _stack_quality_note

    note = _stack_quality_note("raw")
    snapshot = {
        "platform": "flutter", "service": "svc", "title": "t",
        "first_seen_version": "1.0", "last_seen_version": "1.1",
        "first_seen_at": "2026-01-01", "last_seen_at": "2026-01-02",
        "total_events": 5, "stack_trace": "some raw stack",
        "stack_quality_note": note,
    }
    prompt = _build_prompt(snapshot)
    assert note in prompt
    assert "代表性堆栈" in prompt


def test_build_prompt_without_stack_quality_note_uses_default_no_keyerror():
    from app.crashguard.services.analyzer import _build_prompt

    snapshot = {
        "platform": "flutter", "service": "svc", "title": "t",
        "first_seen_version": "1.0", "last_seen_version": "1.1",
        "first_seen_at": "2026-01-01", "last_seen_at": "2026-01-02",
        "total_events": 5, "stack_trace": "some raw stack",
    }
    prompt = _build_prompt(snapshot)  # 没传 stack_quality_note，不应抛 KeyError
    assert "未知" in prompt


def test_followup_prompt_template_untouched():
    """确认追问模板 / 构造函数没有被顺带改到——本次改动范围只在主分析 prompt。"""
    from app.crashguard.services.analyzer import (
        _FOLLOWUP_PROMPT_TEMPLATE, _build_followup_prompt,
    )
    assert "stack_quality_note" not in _FOLLOWUP_PROMPT_TEMPLATE

    snapshot = {
        "platform": "flutter", "service": "svc", "title": "t",
        "first_seen_version": "1.0", "last_seen_version": "1.1",
        "first_seen_at": "2026-01-01", "last_seen_at": "2026-01-02",
        "total_events": 5,
    }
    # 没传 enrichment_block/code_hint/followup_block 也不该抛异常（既有 setdefault 逻辑）
    prompt = _build_followup_prompt(snapshot)
    assert "追问" in prompt
