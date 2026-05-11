"""warmup 完整闭环中的 auto-PR 补建测试。"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_backfills_auto_pr_for_existing_success_analysis(tmp_path, monkeypatch):
    """已有 success 分析但没 PR 时，warmup 应补触发 draft PR。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'warmup.db'}")
    monkeypatch.setenv("CRASHGUARD_PR_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_FEASIBILITY_PR_THRESHOLD", "0.7")

    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()

    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa: F401
    from app.crashguard.models import CrashAnalysis, CrashIssue

    await init_db()

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ddi_existing",
            platform="flutter",
            title="Existing analyzed crash",
            representative_stack="lib/foo.dart:42",
        ))
        ana = CrashAnalysis(
            datadog_issue_id="ddi_existing",
            analysis_run_id="run-existing",
            status="success",
            followup_question="",
            root_cause="root",
            fix_suggestion="fix lib/foo.dart",
            feasibility_score=0.91,
        )
        session.add(ana)
        await session.commit()
        analysis_id = ana.id

    draft_mock = AsyncMock(return_value={
        "ok": True,
        "succeeded": 1,
        "failed": 0,
        "total": 1,
        "prs": [{"ok": True, "pr_url": "https://github.com/o/r/pull/1"}],
    })
    monkeypatch.setattr("app.crashguard.services.pr_drafter.draft_prs_multi", draft_mock)

    from app.crashguard.workers.warmup import _backfill_attention_auto_pr
    result = await _backfill_attention_auto_pr(["ddi_existing"])

    assert result["scanned"] == 1
    assert result["attempted"] == 1
    assert result["created"] == 1
    assert result["failed"] == []
    draft_mock.assert_awaited_once_with(analysis_id, approver="auto")
