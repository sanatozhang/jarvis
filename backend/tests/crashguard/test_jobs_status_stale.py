"""GET /api/crash/jobs/status 的 stale 判定单测。

`api/crash.py::jobs_status` 有一份独立于 job_health_alerter.py 的同款 stale
计算逻辑（两处文档都声明要保持一致）。同样的 bug：只认 status=="success" 判
断"上次存活时间"，一旦某任务的 attention 池被清空后合法地持续返回
status="skipped"，last_success_at 就会停摆，10 分钟后（analyze_tick 间隔 5min
的 2 倍）就被 UI 误判成 stale——2026-08-05 生产实测复现。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

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


def _make_settings(monkeypatch):
    s = MagicMock()
    s.analyze_cron = "*/5 * * * *"
    monkeypatch.setattr(
        "app.crashguard.api.crash.get_crashguard_settings", lambda: s
    )
    return s


@pytest.mark.asyncio
async def test_recent_skipped_heartbeat_not_flagged_stale(patched_session, monkeypatch):
    from app.crashguard.api.crash import jobs_status
    from app.crashguard.models import CrashJobHeartbeat
    from app.db.database import get_session

    _make_settings(monkeypatch)

    async with get_session() as s:
        s.add(CrashJobHeartbeat(
            job_name="analyze_tick",
            fired_at=datetime.utcnow() - timedelta(days=1),
            status="success", duration_ms=100, summary="{}", error="",
        ))
        s.add(CrashJobHeartbeat(
            job_name="analyze_tick",
            fired_at=datetime.utcnow() - timedelta(minutes=2),
            status="skipped", duration_ms=50,
            summary='{"picked": 0, "completed": 0, "remaining": 0}', error="",
        ))
        await s.commit()

    res = await jobs_status()
    item = next(it for it in res["items"] if it["name"] == "analyze_tick")
    assert item["stale"] is False
    assert item["health"] == "ok"


@pytest.mark.asyncio
async def test_only_old_heartbeats_still_flagged_stale(patched_session, monkeypatch):
    """对照组：确认没有把 stale 检测整体关掉——真的没心跳了仍要报 stale。"""
    from app.crashguard.api.crash import jobs_status
    from app.crashguard.models import CrashJobHeartbeat
    from app.db.database import get_session

    _make_settings(monkeypatch)

    async with get_session() as s:
        s.add(CrashJobHeartbeat(
            job_name="analyze_tick",
            fired_at=datetime.utcnow() - timedelta(days=1),
            status="skipped", duration_ms=50, summary="{}", error="",
        ))
        await s.commit()

    res = await jobs_status()
    item = next(it for it in res["items"] if it["name"] == "analyze_tick")
    assert item["stale"] is True
    assert item["health"] == "stale"
