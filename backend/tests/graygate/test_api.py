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


def _settings(feishu_enabled: bool = True, feishu_chat_id: str = "oc_graygate") -> SimpleNamespace:
    return SimpleNamespace(feishu_enabled=feishu_enabled, feishu_chat_id=feishu_chat_id)


def _report(available: bool = True, card: dict | None = None):
    from app.graygate.services.card_builder import GraygateReportCard
    return GraygateReportCard(available=available, card=card if card is not None else {"schema": "2.0"})


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
         patch.object(graygate_api, "build_report_card", new=AsyncMock(return_value=_report(card={"schema": "2.0", "x": "hello"}))) as mock_build, \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock()) as mock_send:
        resp = await api_client.post("/api/graygate/trigger", params={"username": "sanato"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["available"] is True
    assert body["card"] == {"schema": "2.0", "x": "hello"}
    assert body["sent"] is False
    mock_build.assert_awaited_once()
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_false_and_feishu_enabled_sends(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "build_report_card", new=AsyncMock(return_value=_report(card={"schema": "2.0", "x": "hi"}))), \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings(feishu_enabled=True)), \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock(return_value=True)) as mock_send:
        resp = await api_client.post(
            "/api/graygate/trigger", params={"username": "sanato", "dry_run": "false"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is True
    mock_send.assert_awaited_once_with(chat_id="oc_graygate", card={"schema": "2.0", "x": "hi"})


@pytest.mark.asyncio
async def test_dry_run_false_but_feishu_enabled_false_does_not_send(api_client):
    """feishu_enabled 是总闸——dry_run=false 也不能绕过它，返回体说明原因。"""
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "build_report_card", new=AsyncMock(return_value=_report(card={"schema": "2.0", "x": "hi"}))), \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings(feishu_enabled=False)), \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock()) as mock_send:
        resp = await api_client.post(
            "/api/graygate/trigger", params={"username": "sanato", "dry_run": "false"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] is False
    assert body["reason"] == "feishu_enabled=False"
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
         patch.object(graygate_api, "build_report_card", new=AsyncMock(return_value=_report())) as mock_build:
        resp = await api_client.post("/api/graygate/trigger", params={"username": "sanato"})

    assert resp.status_code == 200
    assert resp.json()["target_date"] == "2026-08-18"
    mock_build.assert_awaited_once_with(date(2026, 8, 18))


@pytest.mark.asyncio
async def test_explicit_target_date_is_used(api_client):
    with patch.object(graygate_api.db, "get_user", new=AsyncMock(return_value={"username": "sanato", "role": "admin"})), \
         patch.object(graygate_api, "build_report_card", new=AsyncMock(return_value=_report())) as mock_build:
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
         patch.object(graygate_api, "build_report_card", new=AsyncMock(return_value=_report(available=False))), \
         patch.object(graygate_api, "get_graygate_settings", return_value=_settings(feishu_enabled=True)), \
         patch("app.services.feishu_cli.send_interactive_card", new=AsyncMock()) as mock_send:
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
    with patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set, \
         patch.object(graygate_api, "get_all_focus_versions", new=AsyncMock(
             return_value={"ios": "4.0.302-1050", "android": None}
         )):
        resp = await api_client.post(
            "/api/graygate/focus-version", json={"platform": "ios", "version": "4.0.302-1050"},
        )

    assert resp.status_code == 200
    mock_set.assert_awaited_once_with("ios", "4.0.302-1050")
    assert resp.json() == {"ios": "4.0.302-1050", "android": None}


@pytest.mark.asyncio
async def test_set_focus_version_empty_string_clears_override(api_client):
    with patch.object(graygate_api, "clear_focus_version", new=AsyncMock()) as mock_clear, \
         patch.object(graygate_api, "set_focus_version", new=AsyncMock()) as mock_set, \
         patch.object(graygate_api, "get_all_focus_versions", new=AsyncMock(
             return_value={"ios": None, "android": None}
         )):
        resp = await api_client.post(
            "/api/graygate/focus-version", json={"platform": "ios", "version": ""},
        )

    assert resp.status_code == 200
    mock_clear.assert_awaited_once_with("ios")
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_focus_version_invalid_platform_returns_400(api_client):
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
