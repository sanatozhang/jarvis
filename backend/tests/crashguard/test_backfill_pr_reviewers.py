"""存量 PR reviewer 回填脚本的目标筛选 + dry-run 安全性测试。

回填只应触碰「没有飞书 assignee（reviewer_emails 空）且仍 open/未 review」的 PR
—— 即当初 bot_only / 未 blame 过、GitHub 也没指派的那批。绝不能选中已经
reason=ok（reviewer_emails 非空）的 PR，否则 execute 时会重发飞书卡骚扰。
"""
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401 — 注册 crash_* 表

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    db_mod._session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    yield
    db_mod._session_factory = original


async def _mk(**kw):
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session
    async with get_session() as s:
        pr = CrashPullRequest(**kw)
        s.add(pr)
        await s.commit()
        return pr.id


@pytest.mark.asyncio
async def test_select_targets_only_empty_assignee_open_prs(patched_session):
    from scripts.backfill_pr_reviewers import _select_backfill_target_ids
    from app.db.database import get_session

    # 应选中：bot_only（emails 空）+ open
    want1 = await _mk(analysis_id=1, datadog_issue_id="a", repo="plaud-android",
                      pr_url="https://github.com/Plaud-AI/plaud-android/pull/1",
                      pr_number=1, pr_status="open", reviewer_emails="[]")
    # 应选中：从未 blame 过（reviewer_emails NULL）+ draft
    want2 = await _mk(analysis_id=2, datadog_issue_id="b", repo="plaud-ios",
                      pr_url="https://github.com/Plaud-AI/plaud-ios/pull/2",
                      pr_number=2, pr_status="draft", reviewer_emails=None)
    # 不选：已有 assignee（reason=ok）—— 重跑会重发飞书
    await _mk(analysis_id=3, datadog_issue_id="c", repo="plaud-android",
              pr_url="https://github.com/Plaud-AI/plaud-android/pull/3",
              pr_number=3, pr_status="open", reviewer_emails='["alice@plaud.ai"]')
    # 不选：已 reviewed
    await _mk(analysis_id=4, datadog_issue_id="d", repo="plaud-android",
              pr_url="https://github.com/Plaud-AI/plaud-android/pull/4",
              pr_number=4, pr_status="open", reviewer_emails="[]",
              reviewed_at=datetime.utcnow())
    # 不选：已 merged/closed（pr_status 不在 draft/open）
    await _mk(analysis_id=5, datadog_issue_id="e", repo="plaud-android",
              pr_url="https://github.com/Plaud-AI/plaud-android/pull/5",
              pr_number=5, pr_status="merged", reviewer_emails="[]")

    async with get_session() as s:
        ids = await _select_backfill_target_ids(s)

    assert set(ids) == {want1, want2}


@pytest.mark.asyncio
async def test_run_backfill_dry_run_does_not_assign_or_write(patched_session):
    """dry-run：不调 resolve_and_notify（不写 DB / 不动 GitHub），只预览。"""
    from unittest.mock import patch
    from scripts import backfill_pr_reviewers as bf
    from app.crashguard.services import pr_reviewer
    from app.crashguard.services.pr_reviewer import ReviewerResolution

    pid = await _mk(analysis_id=1, datadog_issue_id="a", repo="plaud-android",
                    pr_url="https://github.com/Plaud-AI/plaud-android/pull/262",
                    pr_number=262, pr_status="open", reviewer_emails="[]")

    res = ReviewerResolution(reason="bot_only",
                             github_candidate_emails=["492934747@qq.com"])

    with patch.object(pr_reviewer, "resolve_reviewers_by_blame", return_value=res), \
         patch.object(pr_reviewer, "_resolve_repo_path_for_pr", return_value="/x"), \
         patch.object(pr_reviewer, "_resolve_email_to_github_login",
                      return_value="realDevLogin"), \
         patch.object(pr_reviewer, "resolve_and_notify") as m_notify:
        summary = await bf.run_backfill(execute=False)

    m_notify.assert_not_called()          # 关键：dry-run 绝不写
    assert summary["targets"] == 1
    assert summary["executed"] == 0
    # 预览里给出 would-assign 的真实 login
    assert summary["preview"][0]["pr_number"] == 262
    assert summary["preview"][0]["would_assign_logins"] == ["realDevLogin"]


@pytest.mark.asyncio
async def test_run_backfill_execute_calls_resolve_and_notify_skip_fallback(patched_session):
    """execute：对每个目标调 resolve_and_notify(skip_fallback=True)，不打扰飞书兜底。"""
    from unittest.mock import patch
    from scripts import backfill_pr_reviewers as bf
    from app.crashguard.services import pr_reviewer

    pid = await _mk(analysis_id=1, datadog_issue_id="a", repo="plaud-android",
                    pr_url="https://github.com/Plaud-AI/plaud-android/pull/262",
                    pr_number=262, pr_status="open", reviewer_emails="[]")

    calls = []

    async def fake_notify(pr_id, skip_fallback=False):
        calls.append((pr_id, skip_fallback))
        return {"sent_count": 0, "fallback": False, "reason": "bot_only"}

    with patch.object(pr_reviewer, "resolve_and_notify", side_effect=fake_notify):
        summary = await bf.run_backfill(execute=True)

    assert calls == [(pid, True)]
    assert summary["executed"] == 1
