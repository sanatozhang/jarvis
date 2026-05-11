"""start_analysis 去重 + force 双轨单测。"""
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


async def _seed(factory, *, issue_id: str, recent_success: bool):
    """种入一个 issue + 可选的最近 success 分析。"""
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
        if recent_success:
            s.add(CrashAnalysis(
                datadog_issue_id=issue_id,
                analysis_run_id="existing-run-1",
                status="success",
                followup_question="",
                root_cause="cached root cause",
                fix_suggestion="cached fix",
                feasibility_score=0.85,
                confidence="high",
                created_at=datetime.utcnow() - timedelta(hours=1),  # 1 小时前
            ))
        await s.commit()


@pytest.mark.asyncio
async def test_dedup_returns_existing_run_id_when_recent_success(patched_session):
    """6 小时窗口内有 success → 复用其 run_id，不开新任务。"""
    from app.crashguard.services.analyzer import start_analysis
    await _seed(patched_session, issue_id="iss-A", recent_success=True)
    with patch(
        "app.crashguard.services.analyzer._run_in_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        run_id = await start_analysis("iss-A", dedup_hours=6)
        assert run_id == "existing-run-1"
        # 关键：不能启动新 task
        mock_bg.assert_not_called()


@pytest.mark.asyncio
async def test_force_true_bypasses_dedup(patched_session):
    """UI 重新分析按钮场景：即使有最近 success 也强制重跑。"""
    from app.crashguard.services.analyzer import start_analysis
    await _seed(patched_session, issue_id="iss-B", recent_success=True)
    with patch(
        "app.crashguard.services.analyzer._run_in_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        run_id = await start_analysis("iss-B", force=True, dedup_hours=6)
        # 强刷：分配新 run_id（不等于 existing）
        assert run_id != "existing-run-1"
        # 后台 task 必须被启动
        mock_bg.assert_called_once()


@pytest.mark.asyncio
async def test_followup_question_bypasses_dedup(patched_session):
    """用户带引导 prompt = 迭代分析，跳过去重直接跑。"""
    from app.crashguard.services.analyzer import start_analysis
    await _seed(patched_session, issue_id="iss-C", recent_success=True)
    with patch(
        "app.crashguard.services.analyzer._run_in_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        run_id = await start_analysis(
            "iss-C", followup_question="重点查空指针",
            dedup_hours=6,
        )
        assert run_id != "existing-run-1"
        mock_bg.assert_called_once()


@pytest.mark.asyncio
async def test_no_dedup_when_no_recent_success(patched_session):
    """没有 success 历史 → 正常启动新任务。"""
    from app.crashguard.services.analyzer import start_analysis
    await _seed(patched_session, issue_id="iss-D", recent_success=False)
    with patch(
        "app.crashguard.services.analyzer._run_in_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        run_id = await start_analysis("iss-D", dedup_hours=6)
        assert run_id  # 新 UUID
        mock_bg.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_window_expired_triggers_new_run(patched_session):
    """旧 success 超出窗口 → 不复用，正常启动新任务。"""
    from app.crashguard.models import CrashIssue, CrashAnalysis
    from app.crashguard.services.analyzer import start_analysis
    async with patched_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="iss-E", platform="flutter", title="t",
            first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
        ))
        s.add(CrashAnalysis(
            datadog_issue_id="iss-E",
            analysis_run_id="stale-run",
            status="success",
            followup_question="",
            root_cause="stale",
            created_at=datetime.utcnow() - timedelta(hours=24),  # 1 天前
        ))
        await s.commit()
    with patch(
        "app.crashguard.services.analyzer._run_in_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        run_id = await start_analysis("iss-E", dedup_hours=6)
        assert run_id != "stale-run"
        mock_bg.assert_called_once()


@pytest.mark.asyncio
async def test_dedup_hours_zero_disables_dedup(patched_session):
    """dedup_hours=0 → 配置禁用去重，总是重跑。"""
    from app.crashguard.services.analyzer import start_analysis
    await _seed(patched_session, issue_id="iss-F", recent_success=True)
    with patch(
        "app.crashguard.services.analyzer._run_in_background",
        new_callable=AsyncMock,
    ) as mock_bg:
        run_id = await start_analysis("iss-F", dedup_hours=0)
        assert run_id != "existing-run-1"
        mock_bg.assert_called_once()


@pytest.mark.asyncio
async def test_analyze_tick_respects_max_per_tick(patched_session):
    """定时分析 tick 必须只挑 max_per_tick 个，不一次性跑全部——防长任务被杀。"""
    from unittest.mock import patch, AsyncMock
    from app.crashguard.workers.scheduler import _run_analyze_tick

    with patch(
        "app.crashguard.workers.warmup._collect_attention_ids",
        new=AsyncMock(return_value=[f"id-{i}" for i in range(20)]),
    ), patch(
        "app.crashguard.services.daily_report._auto_analyze_attention",
        new=AsyncMock(return_value=2),
    ) as mock_analyze:
        res = await _run_analyze_tick(max_per_tick=2)
    # 验证：只传 2 个 id 进 _auto_analyze_attention，不是 20 个
    call_args = mock_analyze.call_args[0][0]
    assert len(call_args) == 2
    assert call_args == ["id-0", "id-1"]
    assert res["picked"] == 2
    assert res["completed"] == 2
    assert res["remaining"] == 18


@pytest.mark.asyncio
async def test_analyze_tick_empty_attention_returns_zero():
    """没 attention 时 tick 优雅退出，不抛异常。"""
    from unittest.mock import patch, AsyncMock
    from app.crashguard.workers.scheduler import _run_analyze_tick

    with patch(
        "app.crashguard.workers.warmup._collect_attention_ids",
        new=AsyncMock(return_value=[]),
    ):
        res = await _run_analyze_tick(max_per_tick=5)
    assert res == {"picked": 0, "completed": 0, "remaining": 0}
