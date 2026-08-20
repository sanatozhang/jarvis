"""/api/crash/pull-requests/stats 单测：合入率 = merged / (merged + closed)，
open/draft 不计入分母（还没决出结果，不该拖累"合入率"）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


async def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'prs.db'}")
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "")
    monkeypatch.setenv("CRASHGUARD_WARMUP_ON_STARTUP", "false")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()
    from app.db.database import init_db
    from app.crashguard import models  # noqa: F401
    await init_db()


async def _seed(issue_id: str, pr_status: str, created_at: datetime):
    from app.db.database import get_session
    from app.crashguard.models import CrashPullRequest

    async with get_session() as session:
        session.add(CrashPullRequest(
            analysis_id=1,
            datadog_issue_id=issue_id,
            pr_status=pr_status,
            created_at=created_at,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_merge_rate_excludes_open_and_draft_from_denominator(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    now = datetime.utcnow()
    await _seed("m1", "merged", now)
    await _seed("m2", "merged", now)
    await _seed("m3", "merged", now)
    await _seed("c1", "closed", now)
    await _seed("o1", "open", now)
    await _seed("d1", "draft", now)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/pull-requests/stats")
        assert r.status_code == 200
        j = r.json()
        at = j["all_time"]
        assert at["created"] == 6
        assert at["merged"] == 3
        assert at["closed"] == 1
        assert at["open_or_draft"] == 2
        # 3 merged / (3 merged + 1 closed) = 0.75，不是 3/6
        assert at["merge_rate"] == 0.75


@pytest.mark.asyncio
async def test_merge_rate_null_when_nothing_decided(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    await _seed("o1", "open", datetime.utcnow())
    await _seed("d1", "draft", datetime.utcnow())

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/pull-requests/stats")
        j = r.json()
        assert j["all_time"]["merge_rate"] is None


@pytest.mark.asyncio
async def test_window_excludes_old_prs(tmp_path, monkeypatch):
    await _setup(tmp_path, monkeypatch)
    now = datetime.utcnow()
    await _seed("recent_merged", "merged", now)
    await _seed("old_merged", "merged", now - timedelta(days=200))

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/crash/pull-requests/stats?days=90")
        j = r.json()
        assert j["all_time"]["merged"] == 2
        assert j["window"]["days"] == 90
        assert j["window"]["merged"] == 1
