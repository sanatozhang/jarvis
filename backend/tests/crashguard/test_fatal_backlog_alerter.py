"""A3: 今日 fatal crash/ANR 积压独立告警单测（2026-08-04，批次 3）。

背景：`_check_fatal_backlog_and_alert`（job_health_alerter.py）复用批次 2 ①.6 通道
同款候选条件（kind in (crash, anr) + fatality=="fatal" + fixable=True + 今日
snapshot），但不设①.6 那样的名额限制——统计"今天总共积压了多少个从未分析过的"，
超过 `fatal_backlog_alert_threshold`（默认 10）才发一条独立飞书告警。

覆盖：
1. 超过阈值 → alerted=True，飞书发送函数被调用一次。
2. 刚好等于/低于阈值 → 不告警。
3. 节流：连续两次调用（间隔 < cooldown）第二次不重复发送。
4. run_job_health_check 顶层 enabled=False / feishu_enabled=False → 整个检查
   （含 fatal backlog）被跳过，不查询也不发送。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401 — 注册 crash_* 表


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine，把全局 _session_factory 指过来。"""
    import app.db.database as db_mod

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


@pytest.fixture(autouse=True)
def _reset_fatal_backlog_throttle():
    """跨测试重置独立节流戳，避免相互污染。"""
    from app.crashguard.services import job_health_alerter as jha
    jha._fatal_backlog_last_alerted_at = None
    yield
    jha._fatal_backlog_last_alerted_at = None


def _patch_settings(monkeypatch, **overrides):
    s = MagicMock()
    s.enabled = True
    s.feishu_enabled = True
    s.fatal_backlog_alert_threshold = 10
    s.fatal_backlog_alert_cooldown_minutes = 240
    s.feishu_alert_email = ""
    s.feishu_target_chat_id = "oc_fake_chat_id"
    s.feishu_target_email = ""
    s.frontend_base_url = "http://localhost:3000"
    s.auto_pr_fixable_platforms = ["android", "ios", "flutter"]
    for k, v in overrides.items():
        setattr(s, k, v)
    monkeypatch.setattr(
        "app.crashguard.services.job_health_alerter.get_crashguard_settings",
        lambda: s,
    )
    return s


async def _seed_fatal_never_analyzed(
    factory, issue_id: str, *, today: date, first_seen_at=None, platform: str = "android",
):
    from app.crashguard.models import CrashIssue, CrashSnapshot
    async with factory() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title=f"Fatal @ {issue_id}",
            platform=platform, kind="crash", fatality="fatal", fixable=True,
            first_seen_at=first_seen_at or datetime.utcnow(),
        ))
        session.add(CrashSnapshot(datadog_issue_id=issue_id, snapshot_date=today, events_count=1))
        await session.commit()


@pytest.mark.asyncio
async def test_over_threshold_alerts_and_sends_card(patched_session, monkeypatch):
    """11 个从未分析过的 fatal issue（阈值 10）→ alerted=True，飞书发送函数调用一次。"""
    today = date.today()
    for i in range(11):
        await _seed_fatal_never_analyzed(patched_session, f"fatal:over-{i}", today=today)

    _patch_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    from app.crashguard.services.job_health_alerter import _check_fatal_backlog_and_alert
    from app.db.database import get_session

    async with get_session() as session:
        res = await _check_fatal_backlog_and_alert(session)

    assert res["count"] == 11
    assert res["alerted"] is True
    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_at_or_below_threshold_does_not_alert(patched_session, monkeypatch):
    """恰好 10 个（等于阈值）→ 不告警。"""
    today = date.today()
    for i in range(10):
        await _seed_fatal_never_analyzed(patched_session, f"fatal:eq-{i}", today=today)

    _patch_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    from app.crashguard.services.job_health_alerter import _check_fatal_backlog_and_alert
    from app.db.database import get_session

    async with get_session() as session:
        res = await _check_fatal_backlog_and_alert(session)

    assert res["count"] == 10
    assert res["alerted"] is False
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_throttle_prevents_duplicate_send_within_cooldown(patched_session, monkeypatch):
    """连续两次调用（间隔 < cooldown）→ 第二次不重复发送。"""
    today = date.today()
    for i in range(11):
        await _seed_fatal_never_analyzed(patched_session, f"fatal:throttle-{i}", today=today)

    _patch_settings(monkeypatch, fatal_backlog_alert_cooldown_minutes=240)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    from app.crashguard.services.job_health_alerter import _check_fatal_backlog_and_alert
    from app.db.database import get_session

    async with get_session() as session:
        res1 = await _check_fatal_backlog_and_alert(session)
    async with get_session() as session:
        res2 = await _check_fatal_backlog_and_alert(session)

    assert res1["alerted"] is True
    assert res2["alerted"] is False
    assert res2.get("throttled") is True
    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_fixable_platform_not_counted_in_backlog(patched_session, monkeypatch):
    """BROWSER 平台的 fatal+fixable+从未分析 issue 不该计入积压数——BROWSER/JS 崩溃
    没有对应 mobile repo，分析后也开不出 PR，跟①/②/①.6 通道的过滤口径必须一致
    （问题 3：批次 2/3 新增的这条查询漏了平台白名单过滤）。"""
    today = date.today()
    # 11 个 BROWSER 平台的候选（超过默认阈值 10），如果平台过滤没生效会误告警。
    for i in range(11):
        await _seed_fatal_never_analyzed(
            patched_session, f"fatal:browser-{i}", today=today, platform="BROWSER",
        )

    _patch_settings(monkeypatch)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)

    from app.crashguard.services.job_health_alerter import _check_fatal_backlog_and_alert
    from app.db.database import get_session

    async with get_session() as session:
        res = await _check_fatal_backlog_and_alert(session)

    assert res["count"] == 0
    assert res["alerted"] is False
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_job_health_check_skips_when_disabled(patched_session, monkeypatch):
    """s.enabled=False → run_job_health_check 顶层直接 skip，fatal backlog 检查
    不查询、不发送。"""
    today = date.today()
    for i in range(11):
        await _seed_fatal_never_analyzed(patched_session, f"fatal:disabled-{i}", today=today)

    s = _patch_settings(monkeypatch, enabled=False)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)
    fb_check_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.job_health_alerter._check_fatal_backlog_and_alert",
        fb_check_mock,
    )

    from app.crashguard.services.job_health_alerter import run_job_health_check

    res = await run_job_health_check()

    assert res == {"skipped": "kill_switch"}
    fb_check_mock.assert_not_called()
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_job_health_check_skips_when_feishu_disabled(patched_session, monkeypatch):
    """s.feishu_enabled=False → 同上，整个检查被跳过。"""
    _patch_settings(monkeypatch, feishu_enabled=False)
    send_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.feishu_cli.send_interactive_card", send_mock)
    fb_check_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.job_health_alerter._check_fatal_backlog_and_alert",
        fb_check_mock,
    )

    from app.crashguard.services.job_health_alerter import run_job_health_check

    res = await run_job_health_check()

    assert res == {"skipped": "kill_switch"}
    fb_check_mock.assert_not_called()
    send_mock.assert_not_called()
