"""AuthMiddleware behavior tests."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.middleware.auth import AuthMiddleware
from app.services.auth_jwt import sign_token


SECRET = "x" * 64


def _make_app(*, sso_enabled: bool, exempt_paths=None):
    app = FastAPI()
    app.add_middleware(
        AuthMiddleware,
        enabled=sso_enabled,
        cookie_name="jarvis_session",
        jwt_secret=SECRET,
        exempt_paths=exempt_paths or ["/api/health", "/api/auth/"],
    )

    @app.get("/api/issues")
    async def issues():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_disabled_lets_request_through():
    app = _make_app(sso_enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/issues")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_enabled_blocks_anon():
    app = _make_app(sso_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/issues")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_enabled_lets_exempt_through():
    app = _make_app(sso_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/api/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_enabled_lets_valid_cookie_through():
    app = _make_app(sso_enabled=True)
    token = sign_token(secret=SECRET, username="u", email="u@plaud.ai",
                       role="user", ttl_seconds=3600)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                            cookies={"jarvis_session": token}) as ac:
        r = await ac.get("/api/issues")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_enabled_rejects_expired_cookie():
    app = _make_app(sso_enabled=True)
    token = sign_token(secret=SECRET, username="u", email="u@plaud.ai",
                       role="user", ttl_seconds=-10)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                            cookies={"jarvis_session": token}) as ac:
        r = await ac.get("/api/issues")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_enabled_rejects_bad_signature():
    app = _make_app(sso_enabled=True)
    token = sign_token(secret="wrong-secret-" * 4, username="u",
                       email="u@plaud.ai", role="user", ttl_seconds=3600)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                            cookies={"jarvis_session": token}) as ac:
        r = await ac.get("/api/issues")
    assert r.status_code == 401
