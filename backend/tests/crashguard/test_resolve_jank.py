"""GET /api/crash/resolve-jank 单测（2026-07-21）。

背景：Datadog 卡顿看板按页面分组，用户盯着某条具体卡顿日志想跳到 Apollo 对应 issue
看 AI 分析。resolver 无状态实时反查：拿 session_id 实时查 Datadog Logs API，用现有
聚合算法（compute_jank_aggregation_key / _parse_jank_event，复用 jank_ingester.py）
算出 issue_id 再 302 跳转 —— 不建映射表、不落库。

覆盖：
1. mock 1 条可解析 iOS 事件 → format=json 断言 count==1 且 issue_id 与手算一致
2. 同输入 format=redirect（默认）→ 302 且 Location 里带 issue=jank:<key>
3. mock 2 条不同 offset 的事件（同 session 命中两个卡顿点）→ format=json count==2
4. mock 空列表 → count==0（json）/ redirect 到 notfound=1（redirect）
5. module+offset+platform 直算兜底路径 → DatadogClient.search_logs_page 不应被调用
6. 既没 session_id 也没 module/offset → 400
7. search_logs_page 抛异常 → 不 500，json 返回 count:0 + error 字段
8. 断言查询 query 用的是 @session_id: 前缀（不是 @session.id:）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401 — 注册 crash_* 表到 Base.metadata


SESSION_ID = "c0748180-1808-4ed4-afcd-835bb6cd9929"


def _raw_event(attrs: dict) -> dict:
    return {"attributes": {"attributes": attrs}}


def _ios_event(offset: str = "0x0000000000e31758") -> dict:
    return _raw_event({
        "os": {"name": "iOS", "version": "18.0"},
        "has_app_frame": True,
        "app_stack_module": "Plaud-Global",
        "app_stack_pc": "0x0000000103e42dd4",
        "app_stack_module_offset": offset,
        "app_stack_module_base": "0x0000000102f1c000",
        "app_stack_frame": "Plaud-Global ???",
        "stack_top_module": "QuartzCore",
        "stack_top_symbol": "some_symbol",
        "stack_trace": "0   QuartzCore ...",
        "version": "4.0.201-941",
        "page": "fileDetail",
    })


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
    s.frontend_base_url = "http://localhost:3000"
    monkeypatch.setattr(
        "app.crashguard.api.crash.get_crashguard_settings", lambda: s,
    )
    return s


def _expected_key(offset: str = "0x0000000000e31758") -> str:
    from app.crashguard.services.jank_ingester import compute_jank_aggregation_key
    return compute_jank_aggregation_key(
        platform="ios", has_app_frame=True,
        app_stack_module="Plaud-Global", app_stack_module_offset=offset,
    )


# ── 1. 单条命中 ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_match_json(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    search_mock = AsyncMock(return_value={"data": [_ios_event()], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await resolve_jank(session_id=SESSION_ID, format="json")
    assert result["count"] == 1
    expected_issue_id = f"jank:{_expected_key()}"
    assert result["matches"][0]["issue_id"] == expected_issue_id
    assert result["matches"][0]["events"] == 1


# ── 2. redirect 分支 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_match_redirect(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    search_mock = AsyncMock(return_value={"data": [_ios_event()], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    resp = await resolve_jank(session_id=SESSION_ID)  # format 默认 redirect
    assert resp.status_code == 302
    expected_issue_id = f"jank:{_expected_key()}"
    location = resp.headers["location"]
    assert f"issue={expected_issue_id}" in location
    assert "fatality=jank" in location


# ── 3. 命中 N>1 个不同卡顿点 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_distinct_issues_json(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    events = [_ios_event(offset="0x0000000000e31758"), _ios_event(offset="0x0000000000f42869")]
    search_mock = AsyncMock(return_value={"data": events, "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await resolve_jank(session_id=SESSION_ID, format="json")
    assert result["count"] == 2
    issue_ids = {m["issue_id"] for m in result["matches"]}
    assert issue_ids == {f"jank:{_expected_key('0x0000000000e31758')}", f"jank:{_expected_key('0x0000000000f42869')}"}


# ── 4. 命中 0 个 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_match_json(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    search_mock = AsyncMock(return_value={"data": [], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await resolve_jank(session_id=SESSION_ID, format="json")
    assert result["count"] == 0
    assert result["matches"] == []


@pytest.mark.asyncio
async def test_no_match_redirect(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    search_mock = AsyncMock(return_value={"data": [], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    resp = await resolve_jank(session_id=SESSION_ID)
    assert resp.status_code == 302
    assert "notfound=1" in resp.headers["location"]


# ── 5. 直算兜底路径（不查 Datadog） ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_direct_compute_fallback_skips_datadog(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    search_mock = AsyncMock(return_value={"data": [_ios_event()], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await resolve_jank(
        session_id=None, platform="ios", module="Plaud-Global",
        offset="0x0000000000e31758", format="json",
    )
    assert result["count"] == 1
    expected_issue_id = f"jank:{_expected_key()}"
    assert result["matches"][0]["issue_id"] == expected_issue_id
    search_mock.assert_not_called()


# ── 6. 参数都没给 → 400 ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_all_params_raises_400(patched_session, monkeypatch):
    from fastapi import HTTPException

    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await resolve_jank(session_id=None, platform=None, module=None, offset=None)
    assert exc_info.value.status_code == 400


# ── 7. Datadog 查询异常不 500 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_datadog_error_does_not_500(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    search_mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    result = await resolve_jank(session_id=SESSION_ID, format="json")
    assert result["count"] == 0
    assert "error" in result

    resp = await resolve_jank(session_id=SESSION_ID)
    assert resp.status_code == 302
    assert "notfound=1" in resp.headers["location"]


# ── 8. 断言用的是 @session_id: 前缀（不是 @session.id:） ─────────────────────

@pytest.mark.asyncio
async def test_query_uses_session_id_facet_not_session_dot_id(patched_session, monkeypatch):
    from app.crashguard.api.crash import resolve_jank

    _patch_settings(monkeypatch)
    search_mock = AsyncMock(return_value={"data": [_ios_event()], "next_cursor": None})
    monkeypatch.setattr(
        "app.crashguard.services.datadog_client.DatadogClient.search_logs_page", search_mock,
    )

    await resolve_jank(session_id=SESSION_ID, format="json")
    _, kwargs = search_mock.call_args
    query = kwargs.get("query", "")
    assert f"@session_id:{SESSION_ID}" in query
    assert "@session.id:" not in query
