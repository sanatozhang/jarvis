"""pr_pending_review_alert 单测：工作日 10:00 积压提醒。"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine。"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _make_settings(monkeypatch, **overrides):
    s = MagicMock()
    s.pr_pending_review_enabled = True
    s.feishu_alert_email = "sanato.zhang@plaud.ai"
    s.pr_reviewer_fallback_email = "sanato.zhang@plaud.ai"
    for k, v in overrides.items():
        setattr(s, k, v)
    monkeypatch.setattr(
        "app.crashguard.config.get_crashguard_settings",
        lambda: s,
    )
    return s


# ---------- card builder ----------

def test_build_pending_review_card_groups_by_repo_and_sorts_by_age():
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    prs = [
        {"pr_url": "u1", "pr_number": 1, "repo": "flutter", "pr_status": "open",
         "reviewer_emails": ["a@plaud.ai"], "age_days": 1},
        {"pr_url": "u2", "pr_number": 2, "repo": "flutter", "pr_status": "draft",
         "reviewer_emails": ["b@plaud.ai", "c@plaud.ai"], "age_days": 5},
        {"pr_url": "u3", "pr_number": 3, "repo": "ios", "pr_status": "open",
         "reviewer_emails": [], "age_days": 0},
    ]
    card = build_pending_review_card(prs)
    assert card["header"]["title"]["content"].startswith("⏰")
    assert "3 条" in card["header"]["title"]["content"]
    # 标题反映总数 + template
    assert card["header"]["template"] in ("blue", "orange", "red")

    # 全文应该包含 PR# 和 link
    import json as _json
    body = _json.dumps(card, ensure_ascii=False)
    assert "#1" in body and "#2" in body and "#3" in body
    assert "u1" in body and "u2" in body and "u3" in body
    # repo 分组：flutter 出现一次（聚合），ios 出现一次
    assert body.count("📦 flutter") == 1
    assert body.count("📦 ios") == 1
    # 未指派的 PR 显示"(未指派)"
    assert "(未指派)" in body


def test_build_pending_review_card_template_escalates_with_count():
    from app.crashguard.services.pr_pending_review_alert import build_pending_review_card
    few = [{"pr_url": "u", "pr_number": i, "repo": "r", "pr_status": "open",
            "reviewer_emails": [], "age_days": 0} for i in range(3)]
    medium = [{"pr_url": "u", "pr_number": i, "repo": "r", "pr_status": "open",
               "reviewer_emails": [], "age_days": 0} for i in range(7)]
    many = [{"pr_url": "u", "pr_number": i, "repo": "r", "pr_status": "open",
             "reviewer_emails": [], "age_days": 0} for i in range(12)]
    assert build_pending_review_card(few)["header"]["template"] == "blue"
    assert build_pending_review_card(medium)["header"]["template"] == "orange"
    assert build_pending_review_card(many)["header"]["template"] == "red"


# ---------- main entry ----------

@pytest.mark.asyncio
async def test_run_alert_disabled_short_circuits(patched_session, monkeypatch):
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    _make_settings(monkeypatch, pr_pending_review_enabled=False)
    res = await run_pending_review_alert()
    assert res["sent"] is False
    assert res["skip_reason"] == "disabled"


@pytest.mark.asyncio
async def test_run_alert_no_target_email(patched_session, monkeypatch):
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    _make_settings(monkeypatch, feishu_alert_email="", pr_reviewer_fallback_email="")
    res = await run_pending_review_alert()
    assert res["sent"] is False
    assert res["skip_reason"] == "no_target_email"


@pytest.mark.asyncio
async def test_run_alert_no_pending_skips_send(patched_session, monkeypatch):
    """库里没有等 review 的 PR 时不发送、不打扰。"""
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    _make_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)
    res = await run_pending_review_alert()
    assert res["pending_count"] == 0
    assert res["sent"] is False
    assert res["skip_reason"] == "no_pending"
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_alert_sends_with_pending_prs(patched_session, monkeypatch):
    """有等 review 的 PR 时发送卡片，merged/closed/已 reviewed 的不计入。"""
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    _make_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    async with get_session() as s:
        # 等 review（应该被列出）
        s.add(CrashPullRequest(
            analysis_id=1, datadog_issue_id="i1", repo="flutter",
            pr_number=100, pr_url="https://example.com/100",
            pr_status="open", reviewer_emails='["alice@plaud.ai"]',
            created_at=datetime.utcnow() - timedelta(days=2),
        ))
        s.add(CrashPullRequest(
            analysis_id=2, datadog_issue_id="i2", repo="ios",
            pr_number=200, pr_url="https://example.com/200",
            pr_status="open", reviewer_emails='[]',
            created_at=datetime.utcnow(),
        ))
        # 已合入（应排除）
        s.add(CrashPullRequest(
            analysis_id=3, datadog_issue_id="i3", repo="flutter",
            pr_number=300, pr_url="https://example.com/300",
            pr_status="merged", merged_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(days=5),
        ))
        # 已关闭（应排除）
        s.add(CrashPullRequest(
            analysis_id=4, datadog_issue_id="i4", repo="ios",
            pr_number=400, pr_url="https://example.com/400",
            pr_status="closed", closed_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(days=3),
        ))
        # 已被 review（应排除）
        s.add(CrashPullRequest(
            analysis_id=5, datadog_issue_id="i5", repo="flutter",
            pr_number=500, pr_url="https://example.com/500",
            pr_status="open", reviewed_at=datetime.utcnow(),
            created_at=datetime.utcnow() - timedelta(days=1),
        ))
        await s.commit()

    res = await run_pending_review_alert()
    assert res["pending_count"] == 2, f"应仅列出 #100 和 #200，结果 {res}"
    assert res["sent"] is True
    send_mock.assert_called_once()

    # 校验卡片内容
    call_kwargs = send_mock.call_args.kwargs
    assert call_kwargs.get("email") == "sanato.zhang@plaud.ai"
    import json as _json
    card_body = _json.dumps(call_kwargs.get("card"), ensure_ascii=False)
    assert "#100" in card_body
    assert "#200" in card_body
    # 已合入/已关闭/已 review 的不能出现
    assert "#300" not in card_body
    assert "#400" not in card_body
    assert "#500" not in card_body


@pytest.mark.asyncio
async def test_run_alert_send_failure_returns_reason(patched_session, monkeypatch):
    from app.crashguard.services.pr_pending_review_alert import run_pending_review_alert
    from app.crashguard.models import CrashPullRequest
    from app.db.database import get_session

    _make_settings(monkeypatch)
    monkeypatch.setattr(
        "app.services.feishu_cli.send_interactive_card",
        AsyncMock(return_value=False),
    )

    async with get_session() as s:
        s.add(CrashPullRequest(
            analysis_id=1, datadog_issue_id="i1", repo="flutter",
            pr_number=100, pr_url="https://example.com/100",
            pr_status="open", reviewer_emails='["alice@plaud.ai"]',
            created_at=datetime.utcnow(),
        ))
        await s.commit()

    res = await run_pending_review_alert()
    assert res["pending_count"] == 1
    assert res["sent"] is False
    assert res["skip_reason"] == "send_failed"
