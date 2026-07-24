"""jank 占位符堆栈回填单测（2026-07-22）。

覆盖 backfill_stuck_jank_issues()：仍是占位符标题的 fixable jank issue 用最近一次
匹配的 Datadog 原始事件重新符号化；已成功符号化的 issue 不被打扰；datadog_api_key
缺失时整体 skip。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)
    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _patch_settings(monkeypatch):
    s = MagicMock()
    s.datadog_api_key = "fake-key"
    s.datadog_app_key = "fake-app-key"
    s.datadog_site = "datadoghq.com"
    s.datadog_service_filter = ""
    s.jank_backfill_lookback_hours = 24
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s,
    )
    return s


def _raw_event(attrs: dict) -> dict:
    return {"attributes": {"attributes": attrs}}


@pytest.mark.asyncio
async def test_backfill_resymbolizes_stuck_issue_using_fresh_event(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues, compute_jank_aggregation_key
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session

    _patch_settings(monkeypatch)

    agg_key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    issue_id = f"jank:{agg_key}"

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title="Jank @ Plaud-Global",  # 占位符：等于原始 module 名
            platform="ios", kind="jank", fatality="jank", fixable=True,
            representative_stack="0   Plaud-Global 0x... + 1",
        ))
        await session.commit()

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame",
        AsyncMock(return_value="SomeClass.someMethod"),
    )
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_stack",
        AsyncMock(return_value="0   Plaud-Global   SomeClass.someMethod\n"),
    )
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr("app.services.repo_router.resolve", lambda platform, version, routing: None)

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))

    assert result["scanned_events"] == 1
    assert result["candidates"] == 1
    assert result["resymbolized"] == 1

    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id))).scalar_one()
    assert row.title == "Jank @ SomeClass.someMethod"


@pytest.mark.asyncio
async def test_backfill_fills_missing_dd_query_attrs_without_resymbolizing(patched_session, monkeypatch):
    """2026-07-24：老 issue（本次修复之前摄入，tags 里没有 dd_query_attrs）即便标题
    已经符号化成功、不需要重新符号化，也该顺路把 dd_query_attrs 补上，让"查看 Datadog"
    链接从"回退成基础 query"变成"精确匹配"——不需要触发昂贵的重新符号化。"""
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues, compute_jank_aggregation_key
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    import json as _json

    _patch_settings(monkeypatch)

    agg_key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    issue_id = f"jank:{agg_key}"

    async with get_session() as session:
        session.add(CrashIssue(
            # 标题已经是符号化成功的样子（不是占位符）——老数据没有 tags.dd_query_attrs
            datadog_issue_id=issue_id, title="Jank @ AlreadyResolved.method",
            platform="ios", kind="jank", fatality="jank", fixable=True,
            representative_stack="already resolved stack", tags="{}",
        ))
        await session.commit()

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )
    resymbolize_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester._symbolicate_new_jank_issue", resymbolize_mock,
    )

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))

    assert result["backfilled_query_attrs"] == 1
    assert result["resymbolized"] == 0
    resymbolize_mock.assert_not_called()

    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id))).scalar_one()
    tags = _json.loads(row.tags)
    assert tags["dd_query_attrs"] == {
        "app_stack_module": "Plaud-Global",
        "app_stack_module_offset": "0x0000000000e31758",
    }


@pytest.mark.asyncio
async def test_backfill_skips_issue_already_symbolized(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues, compute_jank_aggregation_key
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session

    _patch_settings(monkeypatch)

    agg_key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    issue_id = f"jank:{agg_key}"

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title="Jank @ AlreadyResolved.method",  # 不等于原始 module 名 → 已符号化
            platform="ios", kind="jank", fatality="jank", fixable=True,
            representative_stack="already resolved stack",
        ))
        await session.commit()

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )
    resymbolize_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester._symbolicate_new_jank_issue", resymbolize_mock,
    )

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))

    assert result["candidates"] == 0
    assert result["resymbolized"] == 0
    resymbolize_mock.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_skips_when_datadog_key_missing(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues

    s = MagicMock()
    s.datadog_api_key = ""
    monkeypatch.setattr("app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s)
    search_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await backfill_stuck_jank_issues()
    assert result == {"scanned_events": 0, "candidates": 0, "resymbolized": 0}
    search_mock.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_ignores_events_with_no_matching_db_issue(patched_session, monkeypatch):
    """Datadog 返回的事件命中的 issue_id 在 DB 里不存在（例如还没被 ingest_jank_logs
    摄入过）——不应该报错，只是不处理。"""
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues

    _patch_settings(monkeypatch)

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x1",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0", "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))
    assert result["scanned_events"] == 1
    assert result["candidates"] == 0
    assert result["resymbolized"] == 0


@pytest.mark.asyncio
async def test_backfill_skips_issue_at_or_above_max_attempts(patched_session, monkeypatch):
    """一个仍是占位符标题、但 prewarm_attempts 已达上限的 issue 不应被再次重试——
    不计入 candidates，也不调用 _symbolicate_new_jank_issue（否则无限期打
    GitHub 那条容易挂的下载路径）。"""
    from app.crashguard.services.jank_ingester import backfill_stuck_jank_issues, compute_jank_aggregation_key
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session

    s = _patch_settings(monkeypatch)
    s.jank_backfill_max_attempts = 12

    agg_key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset="0x0000000000e31758",
    )
    issue_id = f"jank:{agg_key}"

    async with get_session() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title="Jank @ Plaud-Global",  # 占位符：仍未成功符号化
            platform="ios", kind="jank", fatality="jank", fixable=True,
            representative_stack="0   Plaud-Global 0x... + 1",
            prewarm_attempts=12,  # 已达上限
        ))
        await session.commit()

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": "0x0000000000e31758",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )
    resymbolize_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester._symbolicate_new_jank_issue", resymbolize_mock,
    )

    result = await backfill_stuck_jank_issues(now=datetime(2026, 7, 22, 12, 0, 0))

    assert result["candidates"] == 0
    assert result["resymbolized"] == 0
    resymbolize_mock.assert_not_called()
