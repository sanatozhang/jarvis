"""早晚报 markdown 缓存：报告页读缓存、缺失时现算回填、超过保留期清空。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original_factory = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original_factory


async def _add(factory, report_date: date, markdown: str = "") -> int:
    from app.crashguard.models import CrashDailyReport
    async with factory() as session:
        row = CrashDailyReport(
            report_date=report_date, report_type="morning", top_n=5,
            feishu_message_id="sent", report_payload="{}",
            report_markdown=markdown, created_at=datetime.utcnow(),
        )
        session.add(row)
        await session.commit()
        return row.id


async def _markdown(factory, rid: int) -> str:
    from app.crashguard.models import CrashDailyReport
    async with factory() as session:
        row = (await session.execute(
            select(CrashDailyReport).where(CrashDailyReport.id == rid)
        )).scalar_one()
        return row.report_markdown


@pytest.mark.asyncio
async def test_detail_reads_cache_without_compose(patched_session):
    from app.crashguard.api.crash import get_report_detail
    rid = await _add(patched_session, date.today(), "# 缓存的早报")
    with patch("app.crashguard.services.daily_report.compose_report",
               new_callable=AsyncMock) as compose:
        r = await get_report_detail(rid, window_hours=24)
    assert r["markdown"] == "# 缓存的早报"
    compose.assert_not_called()


@pytest.mark.asyncio
async def test_detail_composes_and_backfills_when_cache_missing(patched_session):
    from app.crashguard.api.crash import get_report_detail
    rid = await _add(patched_session, date.today() - timedelta(days=3))
    with patch("app.crashguard.services.daily_report.compose_report",
               new_callable=AsyncMock, return_value=("# 现算", {})) as compose:
        r = await get_report_detail(rid, window_hours=24)
    assert r["markdown"] == "# 现算"
    compose.assert_awaited_once()
    assert await _markdown(patched_session, rid) == "# 现算"


@pytest.mark.asyncio
async def test_detail_other_windows_always_compose_and_do_not_overwrite(patched_session):
    from app.crashguard.api.crash import get_report_detail
    rid = await _add(patched_session, date.today(), "# 24h 缓存")
    with patch("app.crashguard.services.daily_report.compose_report",
               new_callable=AsyncMock, return_value=("# 7d 视图", {})):
        r = await get_report_detail(rid, window_hours=168)
    assert r["markdown"] == "# 7d 视图"
    assert await _markdown(patched_session, rid) == "# 24h 缓存"


@pytest.mark.asyncio
async def test_detail_does_not_backfill_beyond_retention(patched_session):
    from app.crashguard.api.crash import get_report_detail
    rid = await _add(patched_session, date.today() - timedelta(days=45))
    with patch("app.crashguard.services.daily_report.compose_report",
               new_callable=AsyncMock, return_value=("# 老报告", {})):
        await get_report_detail(rid, window_hours=24)
    assert await _markdown(patched_session, rid) == ""


@pytest.mark.asyncio
async def test_prune_clears_markdown_older_than_retention(patched_session):
    from app.crashguard.services.daily_report import prune_report_markdown_cache
    today = date(2026, 10, 8)
    old = await _add(patched_session, today - timedelta(days=31), "old")
    edge = await _add(patched_session, today - timedelta(days=30), "edge")
    async with patched_session() as session:
        n = await prune_report_markdown_cache(session, today=today)
    assert n == 1
    assert await _markdown(patched_session, old) == ""
    assert await _markdown(patched_session, edge) == "edge"
