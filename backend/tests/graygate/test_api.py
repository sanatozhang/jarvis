"""graygate.api.graygate 单测（POST /api/graygate/trigger）。

The graygate router isn't mounted on `app.main` yet (task-6-brief.md explicitly
keeps `app/main.py` out of scope — see task-6-report.md for the two-line diff
the caller still needs to apply). So these tests build a minimal standalone
FastAPI app with just this router included, rather than reusing the shared
`tests/conftest.py::client` fixture (which imports the full `app.main.app`).

覆盖 task-6-brief.md「验证要求」第 2 条列出的全部场景：
1. 非 admin 用户 → 403。
2. dry_run=True（默认）→ 返回 markdown，send_message 未被调用。
3. dry_run=False 且 feishu_enabled=True → send_message 被调用。
4. dry_run=False 但 feishu_enabled=False → send_message 未被调用，返回体说明原因。
5. target_date 不传 → 使用 BJT 昨天。
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.graygate.api import graygate as graygate_api

_BJT = ZoneInfo("Asia/Shanghai")


@pytest.fixture()
async def api_client():
    app = FastAPI()
    app.include_router(graygate_api.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _settings(
    feishu_enabled: bool = True, feishu_chat_id: str = "oc_graygate",
    api_key_jarvis: str = "test-jarvis-key", api_key_runway: str = "test-runway-key",
    notify_provider: str = "feishu", slack_channel: str = "",
) -> SimpleNamespace:
    # send_enabled 在真实 GraygateSettings 上是读 feishu_enabled 的 property，
    # SimpleNamespace 桩不了 property，这里直接算好同一个值。
    return SimpleNamespace(
        feishu_enabled=feishu_enabled, send_enabled=feishu_enabled,
        feishu_chat_id=feishu_chat_id,
        api_key_jarvis=api_key_jarvis, api_key_runway=api_key_runway,
        notify_provider=notify_provider, slack_channel=slack_channel,
    )


def _data(target_date: date = date(2026, 8, 18), *, worsen: bool = False):
    """一份最小但**真实**的 GraygateReportData。

    刻意不 mock 渲染函数：这个端点的价值就是"发之前先看一眼要发什么"，
    mock 掉渲染等于把唯一值得测的东西测没了。用真数据跑真渲染，顺带
    覆盖两个 provider 的 assemble_*。
    """
    from app.graygate.services.card_builder import GraygateReportData
    return GraygateReportData(
        target_date=target_date,
        d1_day=date(2026, 8, 17),
        version_pattern="4.0.3*",
        worsen_lines=["- 🍎 iOS 大盘 crash-free 掉了 0.3%"] if worsen else [],
        columns_md=["**🍎 iOS**\n\n大盘 ok", "**🤖 Android**\n\n大盘 ok"],
        new_crash_md=None,
        top_crash_md=None,
        top_jank_md=None,
    )


@pytest.mark.asyncio
async def test_non_admin_forbidden(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "bob", "role": "user"})):
        resp = await api_client.post("/api/graygate/trigger", params={"username": "bob"})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unknown_user_forbidden(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value=None)):
        resp = await api_client.post("/api/graygate/trigger", params={"username": "ghost"})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_dry_run_default_returns_markdown_without_sending(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "collect_report_data", new=AsyncMock(return_value=_data())) as mock_build, \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings()), \
         patch.object(graygate_api.notify, "send_daily_report", new=AsyncMock()) as mock_send:
        resp = await api_client.post("/api/graygate/trigger", params={"username": "sanato"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["available"] is True
    assert body["provider"] == "feishu"
    # 真渲染出来的飞书 card（不是桩），至少得是 v2 schema + 带上标题
    assert body["card"]["schema"] == "2.0"
    assert "4.0.3 灰度" in body["card"]["header"]["title"]["content"]
    assert body["blocks"] == []
    assert body["sent"] is False
    mock_build.assert_awaited_once()
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_false_and_feishu_enabled_sends(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "collect_report_data", new=AsyncMock(return_value=_data())), \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings(feishu_enabled=True)), \
         patch.object(graygate_api.notify, "send_daily_report", new=AsyncMock(return_value=True)) as mock_send:
        resp = await api_client.post(
            "/api/graygate/trigger", params={"username": "sanato", "dry_run": "false"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is True
    # 没传 target_date → 端点自己算 BJT 昨天，转发给 send_daily_report 的
    # 必须是**同一个**日期（不能一边预览 D-1、一边发 D）。
    mock_send.assert_awaited_once_with(date.fromisoformat(body["target_date"]))


@pytest.mark.asyncio
async def test_dry_run_false_but_feishu_enabled_false_does_not_send(api_client):
    """send_enabled（历史名 feishu_enabled）是总闸——dry_run=false 也不能绕过它。"""
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "collect_report_data", new=AsyncMock(return_value=_data())), \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings(feishu_enabled=False)), \
         patch.object(graygate_api.notify, "send_daily_report", new=AsyncMock()) as mock_send:
        resp = await api_client.post(
            "/api/graygate/trigger", params={"username": "sanato", "dry_run": "false"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is False
    assert body["reason"] == "send_enabled=False"
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_date_not_passed_defaults_to_bjt_yesterday(api_client, monkeypatch):
    fake_now = datetime(2026, 8, 19, 10, 0, 0, tzinfo=_BJT)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now

    monkeypatch.setattr(graygate_api, "datetime", _FakeDatetime)

    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "collect_report_data", new=AsyncMock(return_value=_data())) as mock_build, \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings()):
        resp = await api_client.post("/api/graygate/trigger", params={"username": "sanato"})

    assert resp.status_code == 200
    assert resp.json()["target_date"] == "2026-08-18"
    mock_build.assert_awaited_once_with(date(2026, 8, 18))


@pytest.mark.asyncio
async def test_explicit_target_date_is_used(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "collect_report_data", new=AsyncMock(return_value=_data())) as mock_build, \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings()):
        resp = await api_client.post(
            "/api/graygate/trigger", params={"username": "sanato", "target_date": "2026-01-01"},
        )

    assert resp.status_code == 200
    assert resp.json()["target_date"] == "2026-01-01"
    mock_build.assert_awaited_once_with(date(2026, 1, 1))


@pytest.mark.asyncio
async def test_invalid_target_date_returns_400(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})):
        resp = await api_client.post(
            "/api/graygate/trigger", params={"username": "sanato", "target_date": "not-a-date"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_dry_run_false_but_report_unavailable_does_not_send(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "collect_report_data", new=AsyncMock(return_value=None)), \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings(feishu_enabled=True)), \
         patch.object(graygate_api.notify, "send_daily_report", new=AsyncMock()) as mock_send:
        resp = await api_client.post(
            "/api/graygate/trigger", params={"username": "sanato", "dry_run": "false"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is False
    assert body["reason"] == "available=False"
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_focus_version_persists_and_returns_both_platforms(api_client):
    """无 SSO 登录态时，必须带合法 X-Graygate-Api-Key 才放行——命中 jarvis 的
    key 就记为 changed_by="jarvis"。"""
    with patch.object(graygate_api, "get_graygate_settings", return_value=_settings()), \
         patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set, \
         patch.object(graygate_api, "get_all_focus_versions", new=AsyncMock(
             return_value={"ios": "4.0.302-1050", "android": None}
         )):
        resp = await api_client.post(
            "/api/graygate/focus-version",
            json={"platform": "ios", "version": "4.0.302-1050"},
            headers={"X-Graygate-Api-Key": "test-jarvis-key"},
        )

    assert resp.status_code == 200
    mock_set.assert_awaited_once_with("ios", "4.0.302-1050", changed_by="jarvis")
    assert resp.json() == {"ios": "4.0.302-1050", "android": None}


@pytest.mark.asyncio
async def test_set_focus_version_runway_key_is_attributed_to_runway(api_client):
    """Runway 的 key 跟 jarvis 的不同，命中后记为 changed_by="runway"。"""
    with patch.object(graygate_api, "get_graygate_settings", return_value=_settings()), \
         patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set, \
         patch.object(graygate_api, "get_all_focus_versions", new=AsyncMock(
             return_value={"ios": "4.0.201-1000", "android": None}
         )):
        resp = await api_client.post(
            "/api/graygate/focus-version",
            json={"platform": "ios", "version": "4.0.201-1000"},
            headers={"X-Graygate-Api-Key": "test-runway-key"},
        )

    assert resp.status_code == 200
    mock_set.assert_awaited_once_with("ios", "4.0.201-1000", changed_by="runway")


@pytest.mark.asyncio
async def test_set_focus_version_without_key_or_sso_is_rejected(api_client):
    """2026-09-16 铁律：没有 SSO 登录态、没带合法 key 的写请求一律 401——
    102 上实测发现有不明调用方靠"完全不鉴权"反复覆盖这个值，改成强制鉴权。"""
    with patch.object(graygate_api, "get_graygate_settings", return_value=_settings()), \
         patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set:
        resp = await api_client.post(
            "/api/graygate/focus-version", json={"platform": "ios", "version": "4.0.302-1050"},
        )

    assert resp.status_code == 401
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_focus_version_wrong_key_is_rejected(api_client):
    with patch.object(graygate_api, "get_graygate_settings", return_value=_settings()), \
         patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set:
        resp = await api_client.post(
            "/api/graygate/focus-version",
            json={"platform": "ios", "version": "4.0.302-1050"},
            headers={"X-Graygate-Api-Key": "not-a-real-key"},
        )

    assert resp.status_code == 401
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_focus_version_empty_string_clears_override(api_client):
    with patch.object(graygate_api, "get_graygate_settings", return_value=_settings()), \
         patch.object(graygate_api, "clear_focus_version", new=AsyncMock()) as mock_clear, \
         patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set, \
         patch.object(graygate_api, "get_all_focus_versions", new=AsyncMock(
             return_value={"ios": None, "android": None}
         )):
        resp = await api_client.post(
            "/api/graygate/focus-version", json={"platform": "ios", "version": ""},
            headers={"X-Graygate-Api-Key": "test-jarvis-key"},
        )

    assert resp.status_code == 200
    mock_clear.assert_awaited_once_with("ios", changed_by="jarvis")
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_focus_version_uses_logged_in_user_email_when_present():
    """有 SSO 登录态（request.state.user）时用邮箱识别，不需要额外带 key——
    浏览器 /settings 页面走这条路径。"""
    app = FastAPI()

    @app.middleware("http")
    async def _fake_auth(request, call_next):
        request.state.user = {"email": "sanato.zhang@plaud.ai", "username": "sanato"}
        return await call_next(request)

    app.include_router(graygate_api.router)
    transport = ASGITransport(app=app)

    with patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set, \
         patch.object(graygate_api, "get_all_focus_versions", new=AsyncMock(
             return_value={"ios": "4.0.302-1050", "android": None}
         )):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/api/graygate/focus-version", json={"platform": "ios", "version": "4.0.302-1050"},
            )

    assert resp.status_code == 200
    mock_set.assert_awaited_once_with("ios", "4.0.302-1050", changed_by="sanato.zhang@plaud.ai")


@pytest.mark.asyncio
async def test_set_focus_version_invalid_platform_returns_400_before_auth_check(api_client):
    """platform 校验先于鉴权——无效 platform 直接 400，即使也没带 key。"""
    resp = await api_client.post(
        "/api/graygate/focus-version", json={"platform": "windows", "version": "1.0"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_get_focus_version_returns_current_state(api_client):
    with patch.object(graygate_api, "get_all_focus_versions", new=AsyncMock(
        return_value={"ios": None, "android": "4.0.302-2010"}
    )):
        resp = await api_client.get("/api/graygate/focus-version")

    assert resp.status_code == 200
    assert resp.json() == {"ios": None, "android": "4.0.302-2010"}


@pytest.mark.asyncio
async def test_focus_version_history_delegates_to_service(api_client):
    fake_rows = [{
        "platform": "ios", "old_value": "4.0.302-1100", "new_value": "4.0.302-1143",
        "changed_by": "sanato.zhang@plaud.ai", "changed_at": "2026-09-15T02:00:00", "notify_sent": True,
    }]
    with patch.object(graygate_api, "get_focus_version_audit_history", new=AsyncMock(
        return_value=fake_rows,
    )) as mock_history:
        resp = await api_client.get("/api/graygate/focus-version/history", params={"platform": "ios"})

    assert resp.status_code == 200
    assert resp.json() == {"items": fake_rows}
    mock_history.assert_awaited_once_with("ios", 50)


@pytest.mark.asyncio
async def test_focus_version_history_invalid_platform_returns_400(api_client):
    resp = await api_client.get("/api/graygate/focus-version/history", params={"platform": "windows"})
    assert resp.status_code == 400
