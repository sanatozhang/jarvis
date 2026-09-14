"""symbol_coverage_monitor 单测。

抓手：19 天符号断供期间上传脚本自报成功，没有任何一方验证"符号到底在不在"——
本模块直接查 crashguard 自己的符号表存储，这里验证三条检查逻辑各自的边界条件。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
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
    s = MagicMock()
    s.enabled = True
    s.feishu_enabled = True
    s.symbol_health_enabled = True
    s.symbol_coverage_top_n_versions = 3
    s.symbol_coverage_min_events = 100
    s.symbol_coverage_alert_cooldown_hours = 24
    s.symbol_coverage_stale_upload_days = 5
    s.symbolication_quality_min_issues = 5
    s.symbolication_quality_raw_rate_threshold = 0.5
    s.frontend_base_url = ""
    s.feishu_alert_email = ""
    s.feishu_target_chat_id = ""
    s.feishu_target_email = ""
    for k, v in overrides.items():
        setattr(s, k, v)
    monkeypatch.setattr(
        "app.crashguard.services.symbol_coverage_monitor.get_crashguard_settings",
        lambda: s,
    )
    from app.crashguard.services import symbol_coverage_monitor as scm
    scm._coverage_last_alerted_at.clear()
    scm._quality_last_alerted_at.clear()
    scm._stale_upload_last_alerted_at = None
    return s


async def _add_issue_and_snapshot(
    session_factory, *, issue_id: str, platform: str, version: str,
    events: int, stack: str = "raw stack, no symbols", fixable: bool = True,
    snapshot_date: date | None = None,
):
    from app.crashguard.models import CrashIssue, CrashSnapshot

    snapshot_date = snapshot_date or date.today()
    async with session_factory() as s:
        s.add(CrashIssue(
            datadog_issue_id=issue_id,
            platform=platform,
            last_seen_version=version,
            representative_stack=stack,
            fixable=fixable,
            kind="crash",
        ))
        s.add(CrashSnapshot(
            datadog_issue_id=issue_id,
            snapshot_date=snapshot_date,
            events_count=events,
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_check_symbol_coverage_flags_missing_package(patched_session, monkeypatch):
    from app.crashguard.services.symbol_coverage_monitor import check_symbol_coverage
    from app.db.database import get_session

    s = _make_settings(monkeypatch)
    await _add_issue_and_snapshot(
        patched_session, issue_id="i1", platform="android", version="4.0.302-1143", events=500,
    )
    monkeypatch.setattr(
        "app.crashguard.services.github_symbols._uploaded_package_dir",
        lambda platform, symbol_type, version: None,
    )

    async with get_session() as session:
        missing = await check_symbol_coverage(session, date.today(), s)

    assert len(missing) == 1
    assert missing[0]["platform"] == "android"
    assert missing[0]["version"] == "4.0.302-1143"


@pytest.mark.asyncio
async def test_check_symbol_coverage_not_flagged_when_package_present(patched_session, monkeypatch):
    from app.crashguard.services.symbol_coverage_monitor import check_symbol_coverage
    from app.db.database import get_session

    s = _make_settings(monkeypatch)
    await _add_issue_and_snapshot(
        patched_session, issue_id="i1", platform="ios", version="4.0.302-1143", events=500,
    )
    monkeypatch.setattr(
        "app.crashguard.services.github_symbols._uploaded_package_dir",
        lambda platform, symbol_type, version: "/data/symbols/ios/dsym/4.0.302-1143",
    )

    async with get_session() as session:
        missing = await check_symbol_coverage(session, date.today(), s)

    assert missing == []


@pytest.mark.asyncio
async def test_check_symbol_coverage_skips_low_traffic_version(patched_session, monkeypatch):
    """events 低于 min_events 的版本不检查——长尾版本没有符号表是正常的，不该告警。"""
    from app.crashguard.services.symbol_coverage_monitor import check_symbol_coverage
    from app.db.database import get_session

    s = _make_settings(monkeypatch, symbol_coverage_min_events=100)
    await _add_issue_and_snapshot(
        patched_session, issue_id="i1", platform="android", version="4.0.302-1143", events=5,
    )
    monkeypatch.setattr(
        "app.crashguard.services.github_symbols._uploaded_package_dir",
        lambda platform, symbol_type, version: None,
    )

    async with get_session() as session:
        missing = await check_symbol_coverage(session, date.today(), s)

    assert missing == []


@pytest.mark.asyncio
async def test_check_stale_symbol_upload_detects_staleness(patched_session, monkeypatch):
    from app.crashguard.models import CrashSymbolPackage
    from app.crashguard.services.symbol_coverage_monitor import check_stale_symbol_upload
    from app.db.database import get_session

    s = _make_settings(monkeypatch, symbol_coverage_stale_upload_days=5)

    async with get_session() as session:
        session.add(CrashSymbolPackage(
            id="pkg1", platform="ios", app_version="4.0.301-1000",
            symbol_type="dsym", file_path="/x", file_name="x.zip",
            created_at=datetime.utcnow() - timedelta(days=19),
        ))
        await session.commit()

    async with get_session() as session:
        result = await check_stale_symbol_upload(session, s)

    assert result is not None
    assert result["age_days"] >= 19


@pytest.mark.asyncio
async def test_check_stale_symbol_upload_ok_when_recent(patched_session, monkeypatch):
    from app.crashguard.models import CrashSymbolPackage
    from app.crashguard.services.symbol_coverage_monitor import check_stale_symbol_upload
    from app.db.database import get_session

    s = _make_settings(monkeypatch, symbol_coverage_stale_upload_days=5)

    async with get_session() as session:
        session.add(CrashSymbolPackage(
            id="pkg1", platform="ios", app_version="4.0.302-1143",
            symbol_type="dsym", file_path="/x", file_name="x.zip",
            created_at=datetime.utcnow() - timedelta(days=1),
        ))
        await session.commit()

    async with get_session() as session:
        result = await check_stale_symbol_upload(session, s)

    assert result is None


@pytest.mark.asyncio
async def test_check_stale_symbol_upload_never_uploaded(patched_session, monkeypatch):
    from app.crashguard.services.symbol_coverage_monitor import check_stale_symbol_upload
    from app.db.database import get_session

    s = _make_settings(monkeypatch)

    async with get_session() as session:
        result = await check_stale_symbol_upload(session, s)

    assert result is not None
    assert result["age_days"] is None


@pytest.mark.asyncio
async def test_check_symbolication_quality_flags_high_raw_rate(patched_session, monkeypatch):
    from app.crashguard.services.symbol_coverage_monitor import check_symbolication_quality
    from app.db.database import get_session

    s = _make_settings(monkeypatch, symbolication_quality_min_issues=5,
                       symbolication_quality_raw_rate_threshold=0.5)
    # 6 个 issue，5 个仍是 raw（未符号化）——rate=0.83 > 0.5 阈值
    for i in range(5):
        await _add_issue_and_snapshot(
            patched_session, issue_id=f"raw{i}", platform="android",
            version="4.0.302-1143", events=10, stack="0x1234 abs raw address",
        )
    await _add_issue_and_snapshot(
        patched_session, issue_id="good1", platform="android",
        version="4.0.302-1143", events=10, stack="MainActivity.kt:42",
    )

    async with get_session() as session:
        result = await check_symbolication_quality(session, date.today(), s)

    assert len(result) == 1
    assert result[0]["platform"] == "android"
    assert result[0]["raw_count"] == 5
    assert result[0]["total"] == 6


@pytest.mark.asyncio
async def test_check_symbolication_quality_skips_small_sample(patched_session, monkeypatch):
    from app.crashguard.services.symbol_coverage_monitor import check_symbolication_quality
    from app.db.database import get_session

    s = _make_settings(monkeypatch, symbolication_quality_min_issues=5)
    for i in range(3):
        await _add_issue_and_snapshot(
            patched_session, issue_id=f"raw{i}", platform="android",
            version="4.0.302-1143", events=10, stack="0x1234 abs raw address",
        )

    async with get_session() as session:
        result = await check_symbolication_quality(session, date.today(), s)

    assert result == []


@pytest.mark.asyncio
async def test_run_symbol_health_check_no_issues_when_healthy(patched_session, monkeypatch):
    from app.crashguard.models import CrashSymbolPackage
    from app.crashguard.services.symbol_coverage_monitor import run_symbol_health_check
    from app.db.database import get_session

    _make_settings(monkeypatch)
    async with get_session() as session:
        session.add(CrashSymbolPackage(
            id="pkg1", platform="ios", app_version="4.0.302-1143",
            symbol_type="dsym", file_path="/x", file_name="x.zip",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    res = await run_symbol_health_check()
    assert res["ok"] is True
    assert res["alerted"] is False


@pytest.mark.asyncio
async def test_run_symbol_health_check_cooldown_dedup(patched_session, monkeypatch):
    """同 (platform, version) 缺失在 cooldown 窗口内不重复告警。"""
    from app.crashguard.services.symbol_coverage_monitor import run_symbol_health_check

    _make_settings(monkeypatch, symbol_coverage_alert_cooldown_hours=24)
    await _add_issue_and_snapshot(
        patched_session, issue_id="i1", platform="android", version="4.0.302-1143", events=500,
    )
    monkeypatch.setattr(
        "app.crashguard.services.github_symbols._uploaded_package_dir",
        lambda platform, symbol_type, version: None,
    )

    first = await run_symbol_health_check()
    assert first["alerted"] is True
    assert len(first["missing_coverage"]) == 1

    second = await run_symbol_health_check()
    assert second["alerted"] is False
