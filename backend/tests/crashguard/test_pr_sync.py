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


def test_default_gate_ci_feedback_close_on_fail_is_false():
    """钉住 2026-05-21 决策：CI 失败默认不再自动 close PR，由人工 review 决定"""
    from app.crashguard.config import get_crashguard_settings
    s = get_crashguard_settings()
    assert s.gate_ci_feedback_close_on_fail is False, (
        "CI 失败应交人工处理；除非显式打开 close_on_fail=True，否则不该自动关 PR"
    )


def test_terminal_statuses_excludes_ci_failed_closed():
    """钉住 2026-05-21 决策：ci_failed_closed 不再是终态——人可能 reopen，
    pr_sync 必须能继续同步 GH 现态回来，否则本地 status 永远漂移。"""
    from app.crashguard.services.pr_sync import _TERMINAL_STATUSES
    assert "ci_failed_closed" not in _TERMINAL_STATUSES
    assert _TERMINAL_STATUSES == {"merged", "closed"}


# ---------- Stage D 接线测试 ----------

@pytest.mark.asyncio
async def test_try_run_review_responder_disabled_short_circuits():
    """默认 pr_review_response_enabled=False → 直接返回 skipped:disabled"""
    from app.crashguard.services.pr_sync import _try_run_review_responder
    from app.crashguard.config import get_crashguard_settings
    s = get_crashguard_settings()
    s.pr_review_response_enabled = False
    try:
        r = await _try_run_review_responder(
            pr_id=999, repo_slug="x/y", pr_number=1,
        )
        assert r["ok"] is True
        assert r["enabled"] is False
        assert r["skipped"] == "disabled"
    finally:
        s.pr_review_response_enabled = False


@pytest.mark.asyncio
async def test_try_run_review_responder_no_actionable_returns_zero(patched_session):
    """enabled=True，但 collect 后无 actionable → dispatched=0，不调 dispatch"""
    from app.crashguard.services import pr_sync
    from app.crashguard.services.pr_review_responder import ReviewItem
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    async with get_session() as session:
        pr = CrashPullRequest(
            analysis_id=1, datadog_issue_id="dummy",
            repo="flutter", branch_name="crashguard/auto-fix/x",
            pr_url="https://github.com/x/y/pull/42", pr_number=42,
            pr_status="open",
        )
        session.add(pr)
        await session.commit()
        pid = pr.id

    s = get_crashguard_settings()
    s.pr_review_response_enabled = True
    try:
        # fetch_pr_reviews 返回空，collect 自然返回空 actionable
        with patch(
            "app.crashguard.services.pr_review_responder.fetch_pr_reviews",
            return_value=(True, [], ""),
        ), patch.object(
            pr_sync, "_try_run_review_responder",
            wraps=pr_sync._try_run_review_responder,
        ), patch(
            "app.crashguard.services.pr_review_responder.dispatch_review_response"
        ) as m_dispatch:
            r = await pr_sync._try_run_review_responder(
                pr_id=pid, repo_slug="x/y", pr_number=42,
            )
        m_dispatch.assert_not_called()
        assert r["ok"] is True
        assert r.get("dispatched") == 0
    finally:
        s.pr_review_response_enabled = False


@pytest.mark.asyncio
async def test_try_run_review_responder_fetch_failed():
    """fetch_pr_reviews 失败 → 立刻返回 stage=fetch_pr_reviews，不进 collect"""
    from app.crashguard.services import pr_sync
    from app.crashguard.config import get_crashguard_settings
    s = get_crashguard_settings()
    s.pr_review_response_enabled = True
    try:
        with patch(
            "app.crashguard.services.pr_review_responder.fetch_pr_reviews",
            return_value=(False, [], "gh err"),
        ):
            r = await pr_sync._try_run_review_responder(
                pr_id=1, repo_slug="x/y", pr_number=42,
            )
        assert r["ok"] is False
        assert r["stage"] == "fetch_pr_reviews"
    finally:
        s.pr_review_response_enabled = False


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


# ─────────── reviews + comments detection (commit X) ───────────

def test_gh_fields_includes_reviews_and_comments():
    """_GH_FIELDS 必须包含人审反馈所需字段，否则下游解析拿不到数据"""
    from app.crashguard.services.pr_sync import _GH_FIELDS
    for required in ("reviews", "comments", "reviewDecision"):
        assert required in _GH_FIELDS, f"{required} missing from _GH_FIELDS"


def test_detect_new_review_activity_returns_post_since_only():
    """只返回 since 之后的新增 reviews / comments，旧的过滤掉"""
    from datetime import datetime
    from app.crashguard.services.pr_sync import _detect_new_review_activity
    since = datetime(2026, 5, 20, 6, 0, 0)
    payload = {
        "reviews": [
            {"author": {"login": "alice"}, "state": "COMMENTED",
             "body": "old comment", "submittedAt": "2026-05-20T05:00:00Z"},
            {"author": {"login": "bob"}, "state": "CHANGES_REQUESTED",
             "body": "fix me", "submittedAt": "2026-05-20T07:00:00Z"},
        ],
        "comments": [
            {"author": {"login": "charlie"}, "body": "lgtm",
             "createdAt": "2026-05-20T08:00:00Z"},
        ],
    }
    items = _detect_new_review_activity(payload, since)
    authors = [i["author"] for i in items]
    assert "alice" not in authors  # 早于 since
    assert authors == ["bob", "charlie"]
    assert items[0]["type"] == "review"
    assert items[0]["state"] == "CHANGES_REQUESTED"
    assert items[1]["type"] == "comment"


def test_detect_new_review_activity_no_since_includes_all():
    """since=None → 返回全部（首次 sync 场景）"""
    from app.crashguard.services.pr_sync import _detect_new_review_activity
    payload = {
        "reviews": [{"author": {"login": "x"}, "state": "APPROVED",
                     "body": "ok", "submittedAt": "2026-05-20T05:00:00Z"}],
        "comments": [],
    }
    items = _detect_new_review_activity(payload, None)
    assert len(items) == 1


def test_detect_new_review_activity_robust_against_malformed():
    """字段缺失 / 类型错乱时不崩"""
    from datetime import datetime
    from app.crashguard.services.pr_sync import _detect_new_review_activity
    payload = {
        "reviews": [None, {}, {"submittedAt": "garbage"}],
        "comments": [{"author": "string-not-dict", "createdAt": "2026-05-20T10:00:00Z",
                      "body": "x"}],
    }
    # author 是字符串而非 dict 也应被接收
    items = _detect_new_review_activity(payload, datetime(2026, 5, 20, 9, 0, 0))
    assert len(items) == 1
    assert items[0]["author"] == "string-not-dict"
