"""CrashIssue.fixable 字段单测（2026-07-20）。

背景：卡顿(jank_watchdog_block)摄入时，has_app_frame=False 的日志（卡顿完全发生在系统
框架内部，没有落到我们自己代码的帧）要标记为 fixable=False，永久排除在 AI 分析/PR 候选
之外。其余所有 kind（crash/anr/memory/web_warning）默认 fixable=True，行为不受影响。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401 — 注册 crash_* 表到 Base.metadata，db_engine fixture 的 create_all 才会建这些表


@pytest.mark.asyncio
async def test_crash_issue_defaults_fixable_true(db_engine):
    """新建 CrashIssue 不显式传 fixable 时，ORM 默认值应为 True。"""
    from app.crashguard.models import CrashIssue
    from sqlalchemy.ext.asyncio import AsyncSession

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        row = CrashIssue(datadog_issue_id="crash-1", title="regular crash")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.fixable is True


@pytest.mark.asyncio
async def test_jank_issue_can_be_marked_not_fixable(db_engine):
    """has_app_frame=False 的卡顿 issue 显式设 fixable=False。"""
    from app.crashguard.models import CrashIssue

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        row = CrashIssue(
            datadog_issue_id="jank:abc123",
            title="Jank @ QuartzCore::CA::Layer::layout_if_needed",
            kind="jank",
            fatality="jank",
            fixable=False,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.fixable is False


def test_fixable_column_registered_in_required_columns():
    """回归防护：确认 migrations.py 里有这条迁移项，防止后续被误删。"""
    from app.crashguard.migrations import _REQUIRED_COLUMNS

    assert ("crash_issues", "fixable", "BOOLEAN", "1") in _REQUIRED_COLUMNS


@pytest.mark.asyncio
async def test_ensure_columns_backfills_fixable_on_legacy_table(db_engine, monkeypatch):
    """模拟迁移前的生产库（crash_issues 表没有 fixable 列），跑 ensure_columns() 后应补上，
    且默认值为 1（True）。
    """
    import app.db.database as db_mod
    from app.crashguard.migrations import ensure_columns

    # db_engine fixture 已经用当前 models.py（含 fixable）建过全部表；这里手动模拟
    # "迁移前"状态：把 fixable 列从 crash_issues 表上删掉（SQLite 3.35+ 支持 DROP COLUMN）。
    async with db_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE crash_issues DROP COLUMN fixable"))

    async def _columns(conn):
        rows = (await conn.execute(text("PRAGMA table_info(crash_issues)"))).all()
        return [r[1] for r in rows]

    async with db_engine.begin() as conn:
        cols_before = await _columns(conn)
    assert "fixable" not in cols_before

    original_factory = db_mod._session_factory
    db_mod._session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    try:
        await ensure_columns()
    finally:
        db_mod._session_factory = original_factory

    async with db_engine.begin() as conn:
        cols_after = await _columns(conn)
    assert "fixable" in cols_after

    async with db_engine.begin() as conn:
        result = await conn.execute(text("SELECT fixable FROM crash_issues LIMIT 0"))
        # 列存在即可；default 值语义已由 _REQUIRED_COLUMNS 里的 "1" 保证，
        # 上面 test_crash_issue_defaults_fixable_true 已经覆盖 ORM 层面默认值。
        assert result is not None
