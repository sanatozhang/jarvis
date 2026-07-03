"""job_health_alerter 单测：disabled 任务跳过 stale/failing 判定。

抓手：evening_daily 下线后 stale 永远叫的真实事故（2026-05-25）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine，把全局 _session_factory 指过来。"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401 — 注册 crash_* 表

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _make_settings(monkeypatch, **overrides):
    """构造 settings mock；evening_enabled 默认 False（模拟真实下线状态）。"""
    s = MagicMock()
    s.enabled = True
    s.feishu_enabled = True
    s.job_health_alert_enabled = True
    s.job_health_alert_cooldown_minutes = 30
    s.job_health_alert_weekend_multiplier = 1
    s.job_health_alert_fail_threshold = 2
    s.job_health_alert_degraded_threshold = 6
    s.job_health_alert_retry_throttle_minutes = 10
    s.frontend_base_url = ""
    s.feishu_alert_email = ""
    s.feishu_target_chat_id = ""
    s.feishu_target_email = ""
    s.core_metric_cron = "*/10 * * * *"
    s.hourly_alert_cron = "5 */3 * * *"
    s.analyze_cron = "*/5 * * * *"
    s.pr_sync_cron = "*/30 * * * *"
    s.pipeline_cron = "0 */4 * * *"
    s.morning_cron = "0 9 * * *"
    s.evening_cron = "0 17 * * *"
    s.top_crash_auto_pr_cron = "0 */6 * * *"
    s.core_metric_enabled = True
    s.hourly_alert_enabled = True
    s.morning_enabled = True
    s.evening_enabled = False    # ← 关键：晚报已下线
    s.top_crash_auto_pr_enabled = True
    s.pr_enabled = True
    for k, v in overrides.items():
        setattr(s, k, v)
    monkeypatch.setattr(
        "app.crashguard.services.job_health_alerter.get_crashguard_settings",
        lambda: s,
    )
    # 重置告警节流字典，避免跨测试污染
    from app.crashguard.services import job_health_alerter as jha
    jha._last_alerted_at.clear()
    jha._last_retried_at.clear()
    jha._consecutive_retry_failures.clear()
    return s


@pytest.mark.asyncio
async def test_disabled_evening_skips_stale_alert(patched_session, monkeypatch):
    """evening_enabled=False 时，即便心跳超过 stale 阈值也不告警。"""
    from app.crashguard.services.job_health_alerter import run_job_health_check
    from app.crashguard.models import CrashJobHeartbeat
    from app.db.database import get_session

    _make_settings(monkeypatch)

    async with get_session() as s:
        s.add(CrashJobHeartbeat(
            job_name="evening_daily",
            fired_at=datetime.utcnow() - timedelta(days=5),
            status="success",
            duration_ms=100,
            summary="{}", error="",
        ))
        await s.commit()

    res = await run_job_health_check()
    assert res["alerted"] is False
    assert "evening_daily" in res["skipped_disabled"]


@pytest.mark.asyncio
async def test_enabled_morning_stale_still_alerts(patched_session, monkeypatch):
    """对照组：morning_enabled=True + 自愈已用尽 → stale 进 unhealthy 发告警；
    evening_daily 即便没心跳也 skipped_disabled。"""
    from app.crashguard.services import job_health_alerter as jha
    from app.crashguard.services.job_health_alerter import run_job_health_check
    from app.crashguard.models import CrashJobHeartbeat
    from app.db.database import get_session

    _make_settings(monkeypatch)
    # 模拟自愈已失败 3 次 — 越过 RETRY_FAILURE_THRESHOLD，直接进告警分支
    jha._consecutive_retry_failures["morning_daily"] = 3

    async with get_session() as s:
        s.add(CrashJobHeartbeat(
            job_name="morning_daily",
            fired_at=datetime.utcnow() - timedelta(days=5),
            status="success",
            duration_ms=100,
            summary="{}", error="",
        ))
        await s.commit()

    monkeypatch.setattr(
        "app.services.feishu_cli.send_interactive_card",
        AsyncMock(return_value=True),
    )

    res = await run_job_health_check()
    assert res["alerted"] is True
    assert "morning_daily" in res["unhealthy_jobs"]
    assert "evening_daily" not in res["unhealthy_jobs"]
    assert "evening_daily" in res["skipped_disabled"]


@pytest.mark.asyncio
async def test_top_crash_auto_pr_skips_when_pr_enabled_false(patched_session, monkeypatch):
    """抓手：2026-07-03 事故——pr_enabled=false 全局暂停开 PR 期间，
    run_top_crash_auto_pr_tick 提前 return 只写 "skipped" 心跳，last_success 不再
    推进。top_crash_auto_pr_enabled 自身仍是 True，若 alerter 只看这一个字段就会
    把这种"预期内的暂停"误判成 stale 并反复告警。此开关同时受
    top_crash_auto_pr_enabled 和全局 pr_enabled 双重把关，任一为 False 都要跳过。
    """
    from app.crashguard.services.job_health_alerter import run_job_health_check
    from app.crashguard.models import CrashJobHeartbeat
    from app.db.database import get_session

    _make_settings(monkeypatch, pr_enabled=False)  # top_crash_auto_pr_enabled 仍是 True

    async with get_session() as s:
        s.add(CrashJobHeartbeat(
            job_name="top_crash_auto_pr",
            fired_at=datetime.utcnow() - timedelta(days=5),
            status="success",
            duration_ms=100,
            summary="{}", error="",
        ))
        await s.commit()

    res = await run_job_health_check()
    assert res["alerted"] is False
    assert "top_crash_auto_pr" in res["skipped_disabled"]
