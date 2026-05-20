"""/api/auth/* full e2e flow with mocked Google."""
from __future__ import annotations

from unittest.mock import patch

import pytest


def test_auth_module_imports():
    """Module loads without ImportError; basic objects present."""
    from app.api import auth
    assert auth.router is not None


def test_exchange_code_helper_is_mockable():
    import inspect
    from app.api.auth import _exchange_code_for_id_token
    assert inspect.iscoroutinefunction(_exchange_code_for_id_token)


def _enable_sso(monkeypatch):
    """Per-test: turn SSO on in the patched settings."""
    from app.config import get_settings
    get_settings().sso.enabled = True


@pytest.mark.asyncio
async def test_login_redirect(client, monkeypatch):
    """GET /api/auth/google/login → 302 to accounts.google.com."""
    _enable_sso(monkeypatch)
    r = await client.get("/api/auth/google/login?next=/issues/123",
                         follow_redirects=False)
    assert r.status_code == 302
    assert "accounts.google.com" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_rejects_invalid_state(client, monkeypatch):
    _enable_sso(monkeypatch)
    r = await client.get("/api/auth/google/callback?code=x&state=garbage",
                         follow_redirects=False)
    assert r.status_code == 302
    assert "error=invalid_state" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_rejects_non_plaud_domain(client, monkeypatch):
    """Email outside SSO_ALLOWED_DOMAINS → /login?error=domain_not_allowed."""
    _enable_sso(monkeypatch)
    from app.services.auth_google import sign_state
    from app.config import get_settings
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/")

    fake_id_token = {"email": "evil@gmail.com", "email_verified": True}
    with patch("app.api.auth._exchange_code_for_id_token", return_value=fake_id_token):
        r = await client.get(
            f"/api/auth/google/callback?code=x&state={state}",
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert "error=domain_not_allowed" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_success_creates_user_and_sets_cookie(client, monkeypatch):
    _enable_sso(monkeypatch)
    from app.services.auth_google import sign_state
    from app.config import get_settings
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/issues/123")

    fake_id_token = {"email": "newcomer@plaud.ai", "email_verified": True}
    with patch("app.api.auth._exchange_code_for_id_token", return_value=fake_id_token):
        r = await client.get(
            f"/api/auth/google/callback?code=x&state={state}",
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
    """Email in ADMIN_EMAILS → role=admin on first login."""
    _enable_sso(monkeypatch)
    from app.config import get_settings
    get_settings().sso.admin_emails_raw = "boss@plaud.ai"

    from app.services.auth_google import sign_state
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/")

    fake_id_token = {"email": "boss@plaud.ai", "email_verified": True}
    with patch("app.api.auth._exchange_code_for_id_token", return_value=fake_id_token):
        await client.get(f"/api/auth/google/callback?code=x&state={state}",
                          follow_redirects=False)

    from app.db import database as db_mod
    user = await db_mod.get_user("boss")
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_callback_preserves_existing_admin(client, monkeypatch):
    """Old admin's email NOT in ADMIN_EMAILS → role stays admin (only-up)."""
    _enable_sso(monkeypatch)
    from app.db import database as db_mod
    await db_mod.upsert_user("oldboss", feishu_email="", role="admin")

    from app.services.auth_google import sign_state
    from app.config import get_settings
    state = sign_state(secret=get_settings().sso.jwt_secret, next_url="/")

    fake_id_token = {"email": "oldboss@plaud.ai", "email_verified": True}
    with patch("app.api.auth._exchange_code_for_id_token", return_value=fake_id_token):
        await client.get(f"/api/auth/google/callback?code=x&state={state}",
                          follow_redirects=False)

    user = await db_mod.get_user("oldboss")
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_me_returns_user_when_authed(client, monkeypatch):
    """Authed GET /api/auth/me returns current user."""
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
