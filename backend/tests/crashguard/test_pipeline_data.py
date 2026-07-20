"""端到端数据流水线测试（不含 AI）"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_run_data_phase_end_to_end(tmp_path, monkeypatch):
    """
    Mock Datadog → 跑完 pipeline.run_data_phase 后:
    - crash_issues 表有 N 条
    - crash_snapshots 表有 N 条且 is_new_in_version 等 flag 已填
    - crash_fingerprints 表关联正确
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'pipe.db'}")
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "test-key")
    monkeypatch.setenv("CRASHGUARD_DATADOG_APP_KEY", "test-app")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()

    from app.db.database import init_db, get_session
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashIssue, CrashSnapshot, CrashFingerprint
    await init_db()

    # Mock DatadogClient.list_issues 返回 2 条
    mock_issues = [
        {
            "id": "ddi_1",
            "attributes": {
                "title": "NullPointerException @ play",
                "service": "plaud_ai",
                "platform": "flutter",
                "first_seen_timestamp": 1714003200000,
                "last_seen_timestamp": 1714176000000,
                "first_seen_version": "1.4.7",
                "last_seen_version": "1.4.7",
                "events_count": 145,
                "users_affected": 23,
                "stack_trace": "NPE\n  at AudioPlayer.play (lib/audio/player.dart:42)\n  at PB._start (lib/audio/playback.dart:18)",
                "tags": {"env": "prod"},
            },
        },
        {
            "id": "ddi_2",
            "attributes": {
                "title": "OOM",
                "service": "plaud_ai",
                "platform": "flutter",
                "first_seen_timestamp": 1714003200000,
                "last_seen_timestamp": 1714176000000,
                "first_seen_version": "1.4.5",
                "last_seen_version": "1.4.7",
                "events_count": 30,
                "users_affected": 8,
                "stack_trace": "OOM\n  at ImgDecoder.decode (lib/image/decoder.dart:99)",
                "tags": {},
            },
        },
    ]

    async def fake_list_issues(self, window_hours=24, page_size=100, tracks="rum", query="*"):
        return mock_issues

    from app.crashguard.services.datadog_client import DatadogClient
    monkeypatch.setattr(DatadogClient, "list_issues", fake_list_issues)
    # run_data_phase 2026-07-20 起会 fire-and-forget 调 jank_ingester，同一个事件循环
    # 里后续的 await 会让这个后台 task 有机会跑起来——不 mock 的话会用 "test-key" 真的
    # 发一次 Datadog Logs Search API 请求。这里跟上面的 list_issues 一样喂假数据。
    async def fake_search_logs_page(self, *, query, from_ms, to_ms, cursor=None, limit=100):
        return {"data": [], "next_cursor": None}
    monkeypatch.setattr(DatadogClient, "search_logs_page", fake_search_logs_page)

    from app.crashguard.workers.pipeline import run_data_phase
    today = date.today()
    result = await run_data_phase(
        today=today,
        latest_release="1.4.7",
        recent_versions=["1.4.4", "1.4.5", "1.4.6"],
    )
    assert result["issues_processed"] == 2
    assert result["snapshots_written"] == 2
    assert result["top_n_count"] >= 1

    async with get_session() as s:
        from sqlalchemy import select, func
        n_issues = (await s.execute(select(func.count()).select_from(CrashIssue))).scalar()
        n_snaps = (await s.execute(
            select(func.count()).select_from(CrashSnapshot).where(CrashSnapshot.snapshot_date == today)
        )).scalar()
        assert n_issues == 2
        assert n_snaps == 2

        ddi1 = (await s.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == "ddi_1")
        )).scalar_one()
        assert ddi1.platform == "flutter"
        assert ddi1.first_seen_version == "1.4.7"
        assert ddi1.stack_fingerprint  # 已计算

        snap1 = (await s.execute(
            select(CrashSnapshot)
            .where(CrashSnapshot.datadog_issue_id == "ddi_1", CrashSnapshot.snapshot_date == today)
        )).scalar_one()
        assert snap1.is_new_in_version is True   # 1.4.7 == latest_release
        assert snap1.crash_free_impact_score > 0


@pytest.mark.asyncio
async def test_qa_build_skipped_by_default_and_captured_when_enabled(tmp_path, monkeypatch):
    """
    2026-07-13：native 4.0 版本号（如 4.0.100-901）第三段固定是 100，正好落进
    qa_version_patch_threshold=100 的默认 QA 内测包判定区间，导致这些真实崩溃
    被 pipeline 100% 丢弹（既不进 crash_issues 也不进 crash_snapshots）。

    qa_capture_enabled 开关：默认 False 维持原有过滤行为；打开后该版本也应正常入库。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'qa_pipe.db'}")
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "test-key")
    monkeypatch.setenv("CRASHGUARD_DATADOG_APP_KEY", "test-app")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()

    from app.db.database import init_db, get_session
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashIssue, CrashSnapshot
    await init_db()

    qa_issue = {
        "id": "ddi_native_qa",
        "attributes": {
            "title": "AppHang",
            "service": "plaud_ios",
            "platform": "ios",
            "first_seen_timestamp": 1714003200000,
            "last_seen_timestamp": 1714176000000,
            "first_seen_version": "4.0.0",
            "last_seen_version": "4.0.100-901",
            "events_count": 55,
            "users_affected": 0,
            "stack_trace": "App Hang",
            "tags": {},
        },
    }

    async def fake_list_issues(self, window_hours=24, page_size=100, tracks="rum", query="*"):
        return [qa_issue]

    from app.crashguard.services.datadog_client import DatadogClient
    monkeypatch.setattr(DatadogClient, "list_issues", fake_list_issues)
    # 同上：避免 fire-and-forget 的 jank_ingester 用 "test-key" 发真实网络请求
    async def fake_search_logs_page(self, *, query, from_ms, to_ms, cursor=None, limit=100):
        return {"data": [], "next_cursor": None}
    monkeypatch.setattr(DatadogClient, "search_logs_page", fake_search_logs_page)

    from app.crashguard.workers.pipeline import run_data_phase
    from sqlalchemy import select, func
    today = date.today()

    # 默认 qa_capture_enabled=False → 被跳过
    result = await run_data_phase(today=today, latest_release="4.0.100", recent_versions=[])
    assert result["issues_processed"] == 0
    assert result["qa_skipped"] == 1
    async with get_session() as s:
        n = (await s.execute(select(func.count()).select_from(CrashIssue))).scalar()
        assert n == 0

    # 打开开关 → 正常入库（用完立刻还原，避免污染后续测试共享的 lru_cache 单例）
    get_crashguard_settings().qa_capture_enabled = True
    try:
        result2 = await run_data_phase(today=today, latest_release="4.0.100", recent_versions=[])
    finally:
        get_crashguard_settings().qa_capture_enabled = False
    assert result2["issues_processed"] == 1
    assert result2["qa_skipped"] == 0
    async with get_session() as s:
        n2 = (await s.execute(select(func.count()).select_from(CrashIssue))).scalar()
        assert n2 == 1
        n_snap = (await s.execute(
            select(func.count()).select_from(CrashSnapshot).where(CrashSnapshot.snapshot_date == today)
        )).scalar()
        assert n_snap == 1


@pytest.mark.asyncio
async def test_run_data_phase_schedules_jank_ingester(tmp_path, monkeypatch):
    """2026-07-20：run_data_phase 每 4h tick 应该 fire-and-forget 调度一次卡顿摄入，
    跟现有的 prewarmer 调度同一种写法（不阻塞主流程，失败不影响 issues_processed 结果）。
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jank_wire.db'}")
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "test-key")
    monkeypatch.setenv("CRASHGUARD_DATADOG_APP_KEY", "test-app")
    from app.config import get_settings
    from app.crashguard.config import get_crashguard_settings
    get_settings.cache_clear()
    get_crashguard_settings.cache_clear()

    from app.db.database import init_db
    from app.crashguard import models  # noqa
    await init_db()

    async def fake_list_issues(self, window_hours=24, page_size=100, tracks="rum", query="*"):
        return []

    from app.crashguard.services.datadog_client import DatadogClient
    monkeypatch.setattr(DatadogClient, "list_issues", fake_list_issues)

    ingest_mock = AsyncMock(return_value={"scanned": 0, "new_issues": 0, "updated_issues": 0})
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester.ingest_jank_logs", ingest_mock,
    )

    from app.crashguard.workers.pipeline import run_data_phase
    today = date.today()
    await run_data_phase(today=today, latest_release="1.0.0", recent_versions=[])
    # fire-and-forget task 需要让事件循环转一圈才会真正执行
    await asyncio.sleep(0)

    ingest_mock.assert_called_once()
