"""/api/auth/* full e2e flow with mocked Feishu."""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_auth_module_imports():
    from app.api import auth
    assert auth.router is not None


def test_exchange_code_helper_is_mockable():
    import inspect
    from app.api.auth import _exchange_code_for_user_info
    assert inspect.iscoroutinefunction(_exchange_code_for_user_info)


def _enable_sso(monkeypatch):
    from app.config import get_settings
    get_settings().sso.enabled = True


@pytest.mark.asyncio
async def test_login_redirect(client, monkeypatch):
    _enable_sso(monkeypatch)
    r = await client.get("/api/auth/feishu/login?next=/issues/123",
                         follow_redirects=False)
    assert r.status_code == 302
    assert "accounts.feishu.cn" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_rejects_invalid_state(client, monkeypatch):
    _enable_sso(monkeypatch)
    r = await client.get("/api/auth/feishu/callback?code=x&state=garbage",
                         follow_redirects=False)
    assert r.status_code == 302
    assert "error=invalid_state" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_rejects_non_plaud_domain(client, monkeypatch):
    _enable_sso(monkeypatch)
    from app.services.auth_feishu import sign_state
    from app.config import get_settings
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/")

    fake_info = {"enterprise_email": "", "email": "evil@gmail.com", "name": "Evil"}
    with patch("app.api.auth._exchange_code_for_user_info", return_value=fake_info):
        r = await client.get(
            f"/api/auth/feishu/callback?code=x&state={state}",
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert "error=domain_not_allowed" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_success_creates_user_and_sets_cookie(client, monkeypatch):
    _enable_sso(monkeypatch)
    from app.services.auth_feishu import sign_state
    from app.config import get_settings
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/issues/123")

    fake_info = {"enterprise_email": "newcomer@plaud.ai", "name": "Newcomer"}
    with patch("app.api.auth._exchange_code_for_user_info", return_value=fake_info):
        r = await client.get(
            f"/api/auth/feishu/callback?code=x&state={state}",
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"].endswith("/issues/123")
    assert "jarvis_session=" in r.headers.get("set-cookie", "")

    from app.db import database as db_mod
    user = await db_mod.get_user("newcomer")
    assert user is not None
    assert user["feishu_email"] == "newcomer@plaud.ai"


@pytest.mark.asyncio
async def test_callback_promotes_admin_from_env(client, monkeypatch):
    _enable_sso(monkeypatch)
    from app.config import get_settings
    get_settings().sso.admin_emails_raw = "boss@plaud.ai"

    from app.services.auth_feishu import sign_state
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/")

    fake_info = {"enterprise_email": "boss@plaud.ai", "name": "Boss"}
    with patch("app.api.auth._exchange_code_for_user_info", return_value=fake_info):
        await client.get(f"/api/auth/feishu/callback?code=x&state={state}",
                          follow_redirects=False)

    from app.db import database as db_mod
    user = await db_mod.get_user("boss")
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_callback_preserves_existing_admin(client, monkeypatch):
    _enable_sso(monkeypatch)
    from app.db import database as db_mod
    await db_mod.upsert_user("oldboss", feishu_email="", role="admin")

    from app.services.auth_feishu import sign_state
    from app.config import get_settings
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/")

    fake_info = {"enterprise_email": "oldboss@plaud.ai", "name": "Boss"}
    with patch("app.api.auth._exchange_code_for_user_info", return_value=fake_info):
        await client.get(f"/api/auth/feishu/callback?code=x&state={state}",
                          follow_redirects=False)

    user = await db_mod.get_user("oldboss")
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_me_returns_user_when_authed(client, monkeypatch):
    _enable_sso(monkeypatch)
    from app.services.auth_jwt import sign_token
    from app.config import get_settings
    token = sign_token(secret=get_settings().sso.jwt_secret,
                       username="u", email="u@plaud.ai", role="user",
                       ttl_seconds=3600)
    from app.db import database as db_mod
    await db_mod.upsert_user("u", feishu_email="u@plaud.ai", role="user")

    r = await client.get("/api/auth/me", cookies={"jarvis_session": token})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "u"
    assert body["email"] == "u@plaud.ai"
    assert body["role"] == "user"
    assert body["feishu_email"] == "u@plaud.ai"


@pytest.mark.asyncio
async def test_logout_clears_cookie(client, monkeypatch):
    _enable_sso(monkeypatch)
    r = await client.post("/api/auth/logout", cookies={"jarvis_session": "anything"})
    assert r.status_code == 204
    set_cookie = r.headers.get("set-cookie", "")
    assert "jarvis_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()
