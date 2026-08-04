"""A2 回归测试：analyze_tick 队头阻塞 bug。

老代码 `_run_analyze_tick` 先按 max_per_tick 切片、再交给 `_auto_analyze_attention` 去重，
导致队头（events 最高、通常早已分析成功）恒占满 picked 名额，池子里真正待分析的 issue
永远轮不到。修复：抽出 `_filter_pending_ids`，在切片前先过滤掉已 success/running/pending
的 issue。

覆盖：
1. `_filter_pending_ids` 本身：过滤已有 fix 分析（success/running/pending，diagnosis 不算）的
   issue，保留顺序。
2. `_run_analyze_tick` 端到端回归：队头 issue 已分析过时，必须挑到后面真正待分析的 issue，
   而不是恒定卡在队头。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


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


async def _seed(factory, *, issue_id: str, with_success_fix_analysis: bool):
    """种入一个 issue，可选附带一条 success 状态的 fix 分析（phase="fix"）。"""
    from app.crashguard.models import CrashIssue, CrashAnalysis
    async with factory() as s:
        s.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="flutter",
            title="test",
            stack_fingerprint="fp",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        ))
        if with_success_fix_analysis:
            s.add(CrashAnalysis(
                datadog_issue_id=issue_id,
                analysis_run_id=f"run-{issue_id}",
                status="success",
                followup_question="",
                phase="fix",
                root_cause="cached root cause",
                created_at=datetime.utcnow(),
            ))
        await s.commit()


@pytest.mark.asyncio
async def test_filter_pending_ids_skips_existing_success_keeps_order(patched_session):
    """issue_A 已有 success fix 分析 → 过滤掉；issue_B/issue_C 待分析 → 保留，顺序不变。"""
    from app.crashguard.services.daily_report import _filter_pending_ids

    await _seed(patched_session, issue_id="issue_A", with_success_fix_analysis=True)
    await _seed(patched_session, issue_id="issue_B", with_success_fix_analysis=False)
    await _seed(patched_session, issue_id="issue_C", with_success_fix_analysis=False)

    result = await _filter_pending_ids(["issue_A", "issue_B", "issue_C"])
    assert result == ["issue_B", "issue_C"]


@pytest.mark.asyncio
async def test_filter_pending_ids_diagnosis_phase_does_not_count_as_analyzed(patched_session):
    """phase="diagnosis" 的分析（Phase 1 深度诊断）不算数，不应把 issue 过滤掉。"""
    from app.crashguard.models import CrashIssue, CrashAnalysis
    from app.crashguard.services.daily_report import _filter_pending_ids

    async with patched_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="issue_D", platform="flutter", title="t",
            first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
        ))
        s.add(CrashAnalysis(
            datadog_issue_id="issue_D",
            analysis_run_id="run-diagnosis-D",
            status="success",
            followup_question="",
            phase="diagnosis",
            created_at=datetime.utcnow(),
        ))
        await s.commit()

    result = await _filter_pending_ids(["issue_D"])
    assert result == ["issue_D"]


@pytest.mark.asyncio
async def test_filter_pending_ids_empty_input_returns_empty_without_db_hit():
    from app.crashguard.services.daily_report import _filter_pending_ids
    assert await _filter_pending_ids([]) == []


async def _seed_terminal(factory, *, issue_id: str, status: str, created_at: datetime):
    """种入一个 issue，附带一条给定 status（failed/empty）+ created_at 的 fix 分析。"""
    from app.crashguard.models import CrashIssue, CrashAnalysis
    async with factory() as s:
        s.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="flutter",
            title="test",
            stack_fingerprint="fp",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        ))
        s.add(CrashAnalysis(
            datadog_issue_id=issue_id,
            analysis_run_id=f"run-{issue_id}",
            status=status,
            followup_question="",
            phase="fix",
            created_at=created_at,
        ))
        await s.commit()


def _patch_dedup_hours(monkeypatch, hours: int):
    """把 daily_report 模块内的 get_crashguard_settings 换成固定 analysis_dedup_hours，
    避免依赖真实 config.yaml/env 里的值，保证测试确定性。"""
    from types import SimpleNamespace
    from app.crashguard.services import daily_report as dr
    monkeypatch.setattr(
        dr, "get_crashguard_settings",
        lambda: SimpleNamespace(analysis_dedup_hours=hours),
    )


@pytest.mark.asyncio
async def test_filter_pending_ids_recent_failed_is_backed_off(patched_session, monkeypatch):
    """I6：最近 analysis_dedup_hours 窗口内有 failed 记录的 issue → 退避，暂不重试。"""
    from app.crashguard.services.daily_report import _filter_pending_ids

    _patch_dedup_hours(monkeypatch, 6)
    await _seed_terminal(
        patched_session, issue_id="issue_failed",
        status="failed", created_at=datetime.utcnow() - timedelta(hours=1),
    )

    result = await _filter_pending_ids(["issue_failed", "issue_never_analyzed"])
    assert "issue_failed" not in result
    assert "issue_never_analyzed" in result


@pytest.mark.asyncio
async def test_filter_pending_ids_recent_empty_is_backed_off(patched_session, monkeypatch):
    """I6：最近窗口内 status="empty" 的 issue 同样应被退避过滤。"""
    from app.crashguard.services.daily_report import _filter_pending_ids

    _patch_dedup_hours(monkeypatch, 6)
    await _seed_terminal(
        patched_session, issue_id="issue_empty",
        status="empty", created_at=datetime.utcnow() - timedelta(hours=1),
    )

    result = await _filter_pending_ids(["issue_empty", "issue_never_analyzed"])
    assert "issue_empty" not in result
    assert "issue_never_analyzed" in result


@pytest.mark.asyncio
async def test_filter_pending_ids_old_failed_outside_window_is_retryable(patched_session, monkeypatch):
    """I6：failed 记录早于退避窗口（analysis_dedup_hours=6，记录是 8 小时前）→ 不再退避，
    重新出现在待分析列表里（窗口过了可以再试一次）。"""
    from app.crashguard.services.daily_report import _filter_pending_ids

    _patch_dedup_hours(monkeypatch, 6)
    await _seed_terminal(
        patched_session, issue_id="issue_stale_failed",
        status="failed", created_at=datetime.utcnow() - timedelta(hours=8),
    )

    result = await _filter_pending_ids(["issue_stale_failed"])
    assert result == ["issue_stale_failed"]


@pytest.mark.asyncio
async def test_filter_pending_ids_zero_dedup_hours_no_crash(patched_session, monkeypatch):
    """边界：analysis_dedup_hours 配置为 0 时不应报错。

    实现沿用 `_auto_analyze_attention`（2210 行）已有的 `int(... or 6)` 写法保持
    风格一致——该写法下配置的 0 是 falsy，`0 or 6` 结果是 6，即退避窗口退化为默认
    6 小时而非真正"关闭"，这是延续既有代码的既定行为、不是本次改动引入的新问题。
    这里只断言这个边界值不会导致异常，且 6 小时前的 failed 记录本就该被退避过滤。
    """
    from app.crashguard.services.daily_report import _filter_pending_ids

    _patch_dedup_hours(monkeypatch, 0)
    await _seed_terminal(
        patched_session, issue_id="issue_recent_failed",
        status="failed", created_at=datetime.utcnow(),
    )

    result = await _filter_pending_ids(["issue_recent_failed"])
    # 0 被 `or 6` 兜底成 6 小时，since = now-6h，"刚刚" created_at 在窗口内 → 仍被退避
    assert result == []


@pytest.mark.asyncio
async def test_run_analyze_tick_skips_head_of_queue_already_analyzed(patched_session):
    """核心回归：队头 issue_A 早已分析过时，tick 必须挑到 issue_B（真正待分析的），
    不能因为「先切片再去重」永远卡在 issue_A 上。

    mock `_collect_attention_ids` 返回 [issue_A, issue_B, issue_C]（按 events 排序）；
    DB 里 issue_A 已是 success，issue_B/issue_C 待分析；max_per_tick=1。
    """
    from app.crashguard.workers.scheduler import _run_analyze_tick

    await _seed(patched_session, issue_id="issue_A", with_success_fix_analysis=True)
    await _seed(patched_session, issue_id="issue_B", with_success_fix_analysis=False)
    await _seed(patched_session, issue_id="issue_C", with_success_fix_analysis=False)

    with patch(
        "app.crashguard.workers.warmup._collect_attention_ids",
        new=AsyncMock(return_value=["issue_A", "issue_B", "issue_C"]),
    ), patch(
        "app.crashguard.services.analyzer.analyze_issue",
        new_callable=AsyncMock,
    ) as mock_analyze_issue:
        res = await _run_analyze_tick(max_per_tick=1)

    # 断言：被喂进 analyze_issue 的是 issue_B，不是恒定的 issue_A
    mock_analyze_issue.assert_called_once_with("issue_B")
    assert res["picked"] == 1
    assert res["completed"] == 1
    # pending 过滤后是 [issue_B, issue_C]，completed=1 → remaining=1
    assert res["remaining"] == 1


@pytest.mark.asyncio
async def test_run_analyze_tick_all_analyzed_returns_zero_without_calling_analyze(patched_session):
    """池子非空但全部已分析完 → 直接返回 0，不再误报 remaining。"""
    from app.crashguard.workers.scheduler import _run_analyze_tick

    await _seed(patched_session, issue_id="issue_A", with_success_fix_analysis=True)

    with patch(
        "app.crashguard.workers.warmup._collect_attention_ids",
        new=AsyncMock(return_value=["issue_A"]),
    ), patch(
        "app.crashguard.services.analyzer.analyze_issue",
        new_callable=AsyncMock,
    ) as mock_analyze_issue:
        res = await _run_analyze_tick(max_per_tick=1)

    mock_analyze_issue.assert_not_called()
    assert res == {"picked": 0, "completed": 0, "remaining": 0}
