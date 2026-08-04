"""analyze_issue() 并发认领单测。

背景：analyze_issue() 此前在 agent 跑完前不写任何 CrashAnalysis 行——两个独立
调度入口（5 分钟 analyze_tick / 4 小时 pipeline）各自查 `_filter_pending_ids`
时都看不到"这个 issue 正在被分析"，会并发重复调用 analyze_issue() 处理同一个
issue（且 `_prepare_workspace` 的 rmtree-then-recreate 在并发时还可能互相删
对方的工作区文件）。修复：analyze_issue() 一开始就落一条 status="running" 的
占位行，跑完后更新同一行（而不是另起新行）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
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


async def _seed_issue(factory, issue_id: str):
    from app.crashguard.models import CrashIssue
    async with factory() as s:
        s.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform="android",
            title="test crash",
            stack_fingerprint="fp",
            representative_stack="stack text",
            first_seen_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
        ))
        await s.commit()


def _fake_output(**overrides):
    from app.crashguard.services.analyzer import AnalysisOutput
    base = dict(
        scenario="s", root_cause="rc", fix_suggestion="fix",
        feasibility_score=0.5, confidence="medium", reproducibility="unknown",
        raw_output="{}", agent_name="claude",
    )
    base.update(overrides)
    return AnalysisOutput(**base)


@pytest.mark.asyncio
async def test_analyze_issue_claims_row_before_agent_runs(patched_session, tmp_path):
    """agent 还没跑完时，DB 里必须已经能查到一条 status=running 的行——
    这是 _filter_pending_ids 判断"是否已被认领"的唯一依据。
    """
    from app.crashguard.models import CrashAnalysis
    from app.crashguard.services import analyzer

    await _seed_issue(patched_session, "iss-running")

    seen_status_mid_run = {}

    async def fake_run_agent(workspace, prompt, is_followup=False):
        # agent 还在"跑"的这一刻，去查一次 DB——模拟另一个调度入口此时的查重
        async with patched_session() as s:
            row = (await s.execute(
                select(CrashAnalysis).where(
                    CrashAnalysis.datadog_issue_id == "iss-running"
                )
            )).scalar_one_or_none()
            seen_status_mid_run["status"] = row.status if row else None
        return _fake_output()

    with patch.object(analyzer, "_build_enrichment_block", new=AsyncMock(return_value="")), \
         patch.object(analyzer, "_prepare_workspace", return_value=tmp_path), \
         patch.object(analyzer, "_run_agent", new=fake_run_agent):
        await analyzer.analyze_issue("iss-running")

    assert seen_status_mid_run["status"] == "running"


@pytest.mark.asyncio
async def test_analyze_issue_updates_same_row_not_new_one(patched_session, tmp_path):
    """跑完后必须只有一条 CrashAnalysis 行（更新占位行），不能多出第二行。"""
    from app.crashguard.models import CrashAnalysis
    from app.crashguard.services import analyzer

    await _seed_issue(patched_session, "iss-single-row")

    with patch.object(analyzer, "_build_enrichment_block", new=AsyncMock(return_value="")), \
         patch.object(analyzer, "_prepare_workspace", return_value=tmp_path), \
         patch.object(analyzer, "_run_agent", new=AsyncMock(return_value=_fake_output())):
        await analyzer.analyze_issue("iss-single-row")

    async with patched_session() as s:
        rows = (await s.execute(
            select(CrashAnalysis).where(
                CrashAnalysis.datadog_issue_id == "iss-single-row"
            )
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].root_cause == "rc"


@pytest.mark.asyncio
async def test_analyze_issue_marks_claim_row_failed_on_agent_exception(patched_session, tmp_path):
    """agent 抛异常时，占位行必须被标 failed（不能永远卡在 running），
    且异常仍要向上抛（_auto_analyze_attention 靠这个 catch 后继续下一个 issue）。
    """
    from app.crashguard.models import CrashAnalysis
    from app.crashguard.services import analyzer

    await _seed_issue(patched_session, "iss-boom")

    with patch.object(analyzer, "_build_enrichment_block", new=AsyncMock(return_value="")), \
         patch.object(analyzer, "_prepare_workspace", return_value=tmp_path), \
         patch.object(analyzer, "_run_agent", new=AsyncMock(side_effect=RuntimeError("agent crashed"))):
        with pytest.raises(RuntimeError, match="agent crashed"):
            await analyzer.analyze_issue("iss-boom")

    async with patched_session() as s:
        rows = (await s.execute(
            select(CrashAnalysis).where(
                CrashAnalysis.datadog_issue_id == "iss-boom"
            )
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "agent crashed" in (rows[0].error or "")


@pytest.mark.asyncio
async def test_filter_pending_ids_excludes_issue_mid_analyze_issue_run(patched_session, tmp_path):
    """端到端验证本次修复的核心目的：analyze_issue() 跑到一半时，
    _filter_pending_ids 必须已经把这个 issue 排除掉。
    """
    from app.crashguard.services import analyzer
    from app.crashguard.services.daily_report import _filter_pending_ids

    await _seed_issue(patched_session, "iss-mid-flight")

    seen_pending_mid_run = {}

    async def fake_run_agent(workspace, prompt, is_followup=False):
        seen_pending_mid_run["pending"] = await _filter_pending_ids(["iss-mid-flight"])
        return _fake_output()

    with patch.object(analyzer, "_build_enrichment_block", new=AsyncMock(return_value="")), \
         patch.object(analyzer, "_prepare_workspace", return_value=tmp_path), \
         patch.object(analyzer, "_run_agent", new=fake_run_agent):
        await analyzer.analyze_issue("iss-mid-flight")

    assert seen_pending_mid_run["pending"] == []
