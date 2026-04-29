"""pr_sync 单测：纯函数 + sync_pr 路径（mock subprocess）。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine，把全局 _session_factory 指过来。"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401  ensure tables registered

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original_factory = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original_factory


# ---------------- 纯函数 ----------------

def test_parse_repo_slug_valid():
    from app.crashguard.services.pr_sync import _parse_repo_slug
    assert _parse_repo_slug("https://github.com/Plaud-AI/plaud-flutter-common/pull/887") \
        == "Plaud-AI/plaud-flutter-common"


def test_parse_repo_slug_invalid():
    from app.crashguard.services.pr_sync import _parse_repo_slug
    assert _parse_repo_slug("") is None
    assert _parse_repo_slug("not a url") is None
    assert _parse_repo_slug("https://gitlab.com/x/y/pull/1") is None


def test_parse_iso_dt_with_z():
    from app.crashguard.services.pr_sync import _parse_iso_dt
    dt = _parse_iso_dt("2026-04-29T08:38:18Z")
    assert isinstance(dt, datetime)
    assert dt.year == 2026 and dt.month == 4 and dt.day == 29
    assert dt.tzinfo is None  # naive


def test_parse_iso_dt_handles_garbage():
    from app.crashguard.services.pr_sync import _parse_iso_dt
    assert _parse_iso_dt("") is None
    assert _parse_iso_dt(None) is None
    assert _parse_iso_dt("not a date") is None


def test_derive_status_merged():
    from app.crashguard.services.pr_sync import _derive_status
    assert _derive_status({"state": "MERGED", "isDraft": False}) == "merged"


def test_derive_status_closed_not_merged():
    from app.crashguard.services.pr_sync import _derive_status
    assert _derive_status({"state": "CLOSED", "isDraft": False}) == "closed"


def test_derive_status_open_draft_vs_ready():
    from app.crashguard.services.pr_sync import _derive_status
    assert _derive_status({"state": "OPEN", "isDraft": True}) == "draft"
    assert _derive_status({"state": "OPEN", "isDraft": False}) == "open"


def test_derive_status_unknown_returns_none():
    from app.crashguard.services.pr_sync import _derive_status
    assert _derive_status({"state": "WEIRD"}) is None
    assert _derive_status({}) is None


# ---------------- sync_pr 端到端（mock subprocess + 真 DB） ----------------

@pytest.mark.asyncio
async def test_sync_pr_terminal_status_skipped(patched_session):
    """已 merged 的 PR：sync_pr 不应再调 gh，直接返回 skipped。"""
    from app.crashguard.services.pr_sync import sync_pr
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as session:
        row = CrashPullRequest(
            analysis_id=1,
            datadog_issue_id="dummy",
            repo="flutter",
            branch_name="x",
            pr_url="https://github.com/o/r/pull/1",
            pr_number=1,
            pr_status="merged",
        )
        session.add(row)
        await session.commit()
        pid = row.id

    # 即使我们 mock subprocess 让它失败——也不应该被调用
    with patch("app.crashguard.services.pr_sync._gh_view") as mock_gh:
        res = await sync_pr(pid)
        mock_gh.assert_not_called()
    assert res["ok"] is True
    assert res.get("skipped") == "terminal"


@pytest.mark.asyncio
async def test_sync_pr_draft_to_merged_writes_back(patched_session):
    """draft PR 在 GitHub 已被 merged → 本地状态应更新为 merged + merged_at 写入。"""
    from app.crashguard.services.pr_sync import sync_pr
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session
    from sqlalchemy import select

    async with get_session() as session:
        row = CrashPullRequest(
            analysis_id=2,
            datadog_issue_id="dummy2",
            repo="flutter",
            branch_name="x",
            pr_url="https://github.com/o/r/pull/2",
            pr_number=2,
            pr_status="draft",
        )
        session.add(row)
        await session.commit()
        pid = row.id

    fake_payload = {
        "state": "MERGED",
        "isDraft": False,
        "mergedAt": "2026-04-29T08:38:18Z",
        "closedAt": "2026-04-29T08:38:18Z",
    }
    with patch(
        "app.crashguard.services.pr_sync._gh_view",
        return_value=(True, fake_payload, ""),
    ):
        res = await sync_pr(pid)

    assert res["ok"] is True
    assert res["changed"] is True
    assert res["old_status"] == "draft"
    assert res["new_status"] == "merged"

    async with get_session() as session:
        row = (await session.execute(
            select(CrashPullRequest).where(CrashPullRequest.id == pid)
        )).scalar_one()
        assert row.pr_status == "merged"
        assert row.merged_at is not None
        assert row.last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_pr_gh_failure_records_synced_at_but_returns_error(patched_session):
    """gh 命令失败时，仍要更新 last_synced_at（避免空转），但返回 ok=False。"""
    from app.crashguard.services.pr_sync import sync_pr
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session
    from sqlalchemy import select

    async with get_session() as session:
        row = CrashPullRequest(
            analysis_id=3,
            datadog_issue_id="dummy3",
            repo="flutter",
            branch_name="x",
            pr_url="https://github.com/o/r/pull/3",
            pr_number=3,
            pr_status="draft",
        )
        session.add(row)
        await session.commit()
        pid = row.id

    with patch(
        "app.crashguard.services.pr_sync._gh_view",
        return_value=(False, {}, "gh CLI not installed"),
    ):
        res = await sync_pr(pid)

    assert res["ok"] is False
    assert "gh CLI" in res["error"]

    async with get_session() as session:
        row = (await session.execute(
            select(CrashPullRequest).where(CrashPullRequest.id == pid)
        )).scalar_one()
        assert row.pr_status == "draft"           # 没动
        assert row.last_synced_at is not None     # 但记录了尝试


@pytest.mark.asyncio
async def test_sync_all_open_prs_skips_terminal(patched_session):
    """批量同步：merged 的不应被 _gh_view 调用。"""
    from app.crashguard.services.pr_sync import sync_all_open_prs
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as session:
        for status, num in [("merged", 100), ("closed", 101), ("draft", 102), ("open", 103)]:
            session.add(CrashPullRequest(
                analysis_id=99,
                datadog_issue_id=f"d{num}",
                repo="flutter",
                branch_name=f"b{num}",
                pr_url=f"https://github.com/o/r/pull/{num}",
                pr_number=num,
                pr_status=status,
            ))
        await session.commit()

    fake = {"state": "MERGED", "isDraft": False, "mergedAt": "2026-04-29T08:38:18Z", "closedAt": None}
    with patch(
        "app.crashguard.services.pr_sync._gh_view",
        return_value=(True, fake, ""),
    ) as mock_gh:
        res = await sync_all_open_prs()
        # 只有 draft + open 应被调用（2 次）
        assert mock_gh.call_count == 2

    assert res["checked"] == 2
    assert res["changed"] == 2
