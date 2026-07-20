"""categorizer.py::classify_kind() 卡顿(jank) 识别单测（2026-07-20）。

背景：102 生产环境实测发现，`migrations.py::_backfill_kind()` 每次应用启动都会跑一遍
全表重分类（幂等设计，用于修正历史误分类），但 classify_kind() 不认识 jank_ingester.py
生成的 "Jank @ ..." 标题，落到默认的 "crash" 分支——结果是每次容器重启，所有卡顿 issue
的 kind 都被悄悄改回 "crash"，混入正常崩溃统计（8 个卡顿全部复现了这个问题）。
"""
from __future__ import annotations

import pytest


def test_classify_kind_recognizes_jank_title():
    from app.crashguard.services.categorizer import classify_kind

    assert classify_kind("Jank @ Plaud-Global", "ios", "plaud_ios") == "jank"
    assert classify_kind("Jank @ android.os::android.os.Trace.traceEnd", "android", "plaud_android") == "jank"


def test_classify_kind_jank_not_confused_with_anr():
    """卡顿标题不应该被 ANR 正则误命中（反之亦然，两者是独立分类）。"""
    from app.crashguard.services.categorizer import classify_kind

    assert classify_kind("AppHang @ dart::Utils::VSNPrint", "ios", "plaud_ios") == "anr"
    assert classify_kind("Jank @ Plaud-Global", "ios", "plaud_ios") == "jank"


@pytest.mark.asyncio
async def test_backfill_kind_preserves_jank_across_reruns(db_engine, monkeypatch):
    """回归测试：_backfill_kind() 是幂等的，重复跑不应该把 jank 冲回 crash
    （102 生产实测过这个 bug——每次容器重启都会复现）。
    """
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401
    from app.crashguard.models import CrashIssue
    from app.crashguard.migrations import _backfill_kind
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original_factory = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    try:
        async with factory() as session:
            session.add(CrashIssue(
                datadog_issue_id="jank:test1", title="Jank @ Plaud-Global",
                platform="ios", service="plaud_ios", kind="jank", fatality="jank",
            ))
            await session.commit()

        # 模拟应用重启：跑两次（第一次可能是"首次识别"，第二次验证幂等不回退）
        await _backfill_kind()
        await _backfill_kind()

        async with factory() as session:
            row = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == "jank:test1")
            )).scalar_one()
        assert row.kind == "jank"
    finally:
        db_mod._session_factory = original_factory
