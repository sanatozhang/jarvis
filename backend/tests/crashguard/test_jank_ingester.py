"""jank_ingester.py 单测（2026-07-20）。

覆盖：聚合键计算（compute_jank_aggregation_key）、单条日志解析（_parse_jank_event）、
以及完整摄入循环 ingest_jank_logs()（upsert crash_issues/crash_snapshots + 新 issue
触发符号化 + cursor 持久化 + 分页）。
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401 — 注册 crash_* 表到 Base.metadata


# ── compute_jank_aggregation_key ─────────────────────────────────────────────

def test_ios_key_uses_module_and_pc():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_pc="0x0000000103e42dd4",
    )
    assert len(key) == 16
    # 同一地址必须算出同一个键（同一处卡顿反复出现要落到同一个 issue）
    key2 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_pc="0x0000000103e42dd4",
    )
    assert key == key2


def test_ios_key_differs_for_different_pc():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key1 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_pc="0x0000000103e42dd4",
    )
    key2 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_pc="0x0000000105cec708",
    )
    assert key1 != key2


def test_android_key_uses_frame_text():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key1 = compute_jank_aggregation_key(
        platform="android", has_app_frame=True,
        app_stack_frame="ai.plaud.android.payment.k.a",
    )
    key2 = compute_jank_aggregation_key(
        platform="android", has_app_frame=True,
        app_stack_frame="ai.plaud.android.payment.k.a",
    )
    key3 = compute_jank_aggregation_key(
        platform="android", has_app_frame=True,
        app_stack_frame="ai.plaud.android.markdown.render.MarkdownViewKt",
    )
    assert key1 == key2
    assert key1 != key3


def test_no_app_frame_uses_top_module_and_symbol_bucket():
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key

    key1 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=False,
        stack_top_module="QuartzCore", stack_top_symbol="CA::Layer::layout_if_needed",
    )
    key2 = compute_jank_aggregation_key(
        platform="ios", has_app_frame=False,
        stack_top_module="QuartzCore", stack_top_symbol="CA::Layer::layout_if_needed",
    )
    key3 = compute_jank_aggregation_key(
        platform="android", has_app_frame=False,
        stack_top_module="android.os", stack_top_symbol="Handler.dispatchMessage",
    )
    assert key1 == key2
    assert key1 != key3


# ── _parse_jank_event ────────────────────────────────────────────────────────

def _raw_event(attrs: dict) -> dict:
    return {"attributes": {"attributes": attrs}}


def test_parse_ios_event_with_app_frame():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "iOS", "version": "26.0.1"},
        "has_app_frame": True,
        "app_stack_module": "Plaud-Global",
        "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_base": "0x0000000102f1c000",
        "app_stack_frame": "Plaud-Global ???",
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "some_symbol",
        "stack_trace": "0   QuartzCore ...",
        "version": "4.0.201-941",
    }))
    assert parsed is not None
    assert parsed["platform"] == "ios"
    assert parsed["has_app_frame"] is True
    assert parsed["frame_label"] == "Plaud-Global"
    assert parsed["issue_id"].startswith("jank:")
    assert parsed["app_version"] == "4.0.201-941"


def test_parse_android_event_with_app_frame():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "Android", "version": "14"},
        "has_app_frame": True,
        "app_stack_frame": "ai.plaud.android.payment.k.a",
        "stack_top_module": "android.os",
        "stack_top_symbol": "Handler.dispatchMessage",
        "stack_trace": "  at ...",
        "version": None,
    }))
    assert parsed is not None
    assert parsed["platform"] == "android"
    assert parsed["frame_label"] == "ai.plaud.android.payment.k.a"


def test_parse_event_without_app_frame_falls_back_to_top_symbol():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    parsed = _parse_jank_event(_raw_event({
        "os": {"name": "iOS"},
        "has_app_frame": False,
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "CA::Layer::layout_if_needed",
    }))
    assert parsed is not None
    assert parsed["has_app_frame"] is False
    assert parsed["frame_label"] == "QuartzCore::CA::Layer::layout_if_needed"


def test_parse_event_missing_os_returns_none():
    from app.crashguard.services.jank_ingester import _parse_jank_event

    assert _parse_jank_event(_raw_event({"has_app_frame": True})) is None
    assert _parse_jank_event({}) is None
    assert _parse_jank_event(_raw_event({"os": {"name": ""}})) is None


# ── ingest_jank_logs（完整摄入循环） ──────────────────────────────────────────

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
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s,
    )
    return s


def _patch_no_op_symbolication_deps(monkeypatch):
    """符号化路径不是本测试重点：resolve/symbolicate 都 no-op，只验证摄入/upsert 逻辑。"""
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame",
        AsyncMock(return_value="SomeClass.someMethod"),
    )
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr(
        "app.services.repo_router.resolve",
        lambda platform, version, routing: None,
    )


@pytest.mark.asyncio
async def test_ingest_creates_new_fixable_issue_and_symbolicates(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue, CrashSnapshot
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    event = _raw_event({
        "os": {"name": "iOS"},
        "has_app_frame": True,
        "app_stack_module": "Plaud-Global",
        "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_base": "0x0000000102f1c000",
        "stack_trace": "0   Plaud-Global 0x... + 123",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    now = datetime(2026, 7, 20, 12, 0, 0)
    result = await ingest_jank_logs(now=now)

    assert result == {"scanned": 1, "new_issues": 1, "updated_issues": 0}

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()
        snap = (await s.execute(select(CrashSnapshot))).scalar_one()

    assert issue.kind == "jank"
    assert issue.fatality == "jank"
    assert issue.fixable is True
    assert issue.platform == "ios"
    assert issue.total_events == 1
    assert issue.representative_stack == "SomeClass.someMethod"  # 符号化写回
    assert issue.prewarm_attempts == 1
    assert snap.events_count == 1
    assert snap.snapshot_date == date(2026, 7, 20)


@pytest.mark.asyncio
async def test_ingest_marks_no_app_frame_issue_unfixable_and_skips_symbolication(
    patched_session, monkeypatch,
):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    symbolicate_mock = AsyncMock(return_value="should not be called")
    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame", symbolicate_mock,
    )

    event = _raw_event({
        "os": {"name": "ios"},
        "has_app_frame": False,
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "layout",
        "stack_trace": "0   QuartzCore 0x... + 1",
        "version": "4.0.201-941",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    await ingest_jank_logs(now=datetime(2026, 7, 20, 12, 0, 0))

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()
    assert issue.fixable is False
    symbolicate_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_increments_existing_issue_and_snapshot(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue, CrashSnapshot
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    event = _raw_event({
        "os": {"name": "Android"},
        "has_app_frame": True,
        "app_stack_frame": "ai.plaud.android.payment.k.a",
        "stack_trace": "  at ...",
        "version": "4.0.201-941",
    })
    search_mock = AsyncMock(return_value={"data": [event], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    # 第一次摄入：新建
    await ingest_jank_logs(now=datetime(2026, 7, 20, 8, 0, 0))
    # 第二次摄入（同一天，同一处卡顿再次出现）：应该累加而不是新建
    await ingest_jank_logs(now=datetime(2026, 7, 20, 9, 0, 0))

    async with get_session() as s:
        issues = (await s.execute(select(CrashIssue))).scalars().all()
        snaps = (await s.execute(select(CrashSnapshot))).scalars().all()

    assert len(issues) == 1
    assert issues[0].total_events == 2
    assert len(snaps) == 1
    assert snaps[0].events_count == 2


@pytest.mark.asyncio
async def test_ingest_paginates_until_no_next_cursor(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    def _event(pc: str) -> dict:
        return _raw_event({
            "os": {"name": "iOS"}, "has_app_frame": True,
            "app_stack_module": "Plaud-Global", "app_stack_pc": pc,
            "app_stack_module_base": "0x0", "version": "4.0.0-1",
        })

    page1 = {"data": [_event("0x1")], "next_cursor": "cursor-2"}
    page2 = {"data": [_event("0x2")], "next_cursor": None}
    search_mock = AsyncMock(side_effect=[page1, page2])
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await ingest_jank_logs(now=datetime(2026, 7, 20, 12, 0, 0))

    assert result["scanned"] == 2
    assert result["new_issues"] == 2  # 两个不同 pc → 两个不同 issue
    assert search_mock.call_count == 2
    # 第二次调用应该带上第一页返回的 cursor
    assert search_mock.call_args_list[1].kwargs["cursor"] == "cursor-2"

    async with get_session() as s:
        count = len((await s.execute(select(CrashIssue))).scalars().all())
    assert count == 2


@pytest.mark.asyncio
async def test_ingest_persists_and_reuses_cursor(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs

    _patch_settings(monkeypatch)
    _patch_no_op_symbolication_deps(monkeypatch)

    search_mock = AsyncMock(return_value={"data": [], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    first_now = datetime(2026, 7, 20, 12, 0, 0)
    await ingest_jank_logs(now=first_now)
    first_call_from_ms = search_mock.call_args_list[0].kwargs["from_ms"]
    # 首次运行无历史 cursor，应该用默认回看窗口
    expected_first_from = int(first_now.timestamp() * 1000) - 4 * 3600 * 1000
    assert first_call_from_ms == expected_first_from

    second_now = datetime(2026, 7, 20, 16, 0, 0)
    await ingest_jank_logs(now=second_now)
    second_call_from_ms = search_mock.call_args_list[1].kwargs["from_ms"]
    # 第二次应该复用第一次的 to_ms 作为 from_ms（cursor 持久化），而不是重新回看4h
    assert second_call_from_ms == int(first_now.timestamp() * 1000)


@pytest.mark.asyncio
async def test_ingest_skips_when_datadog_key_missing(patched_session, monkeypatch):
    from app.crashguard.services.jank_ingester import ingest_jank_logs

    s = MagicMock()
    s.datadog_api_key = ""
    monkeypatch.setattr(
        "app.crashguard.services.jank_ingester.get_crashguard_settings", lambda: s,
    )
    search_mock = AsyncMock()
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await ingest_jank_logs()
    assert result == {"scanned": 0, "new_issues": 0, "updated_issues": 0}
    search_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_continues_when_symbolication_raises(patched_session, monkeypatch):
    """符号化异常不能中断整个摄入循环——issue 照样建，只是符号化失败记录下来。"""
    from app.crashguard.services.jank_ingester import ingest_jank_logs
    from app.crashguard.models import CrashIssue
    from app.db.database import get_session
    from sqlalchemy import select

    _patch_settings(monkeypatch)
    monkeypatch.setattr("app.config.get_repo_routing", lambda: {})
    monkeypatch.setattr(
        "app.services.repo_router.resolve", lambda platform, version, routing: None,
    )

    async def _boom(**kwargs):
        raise RuntimeError("symbol package download exploded")

    monkeypatch.setattr(
        "app.crashguard.services.symbolication.symbolicate_jank_frame", _boom,
    )

    event = _raw_event({
        "os": {"name": "iOS"}, "has_app_frame": True,
        "app_stack_module": "Plaud-Global", "app_stack_pc": "0x1",
        "app_stack_module_base": "0x0", "version": "4.0.0-1",
    })
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page",
        AsyncMock(return_value={"data": [event], "next_cursor": None}),
    )

    result = await ingest_jank_logs(now=datetime(2026, 7, 20, 12, 0, 0))
    assert result["new_issues"] == 1

    async with get_session() as s:
        issue = (await s.execute(select(CrashIssue))).scalar_one()
    assert issue.prewarm_attempts == 1
    assert "symbol package download exploded" in issue.prewarm_last_error
    # representative_stack 保留摄入时的原始占位（未被符号化覆盖）
    assert issue.representative_stack == ""
