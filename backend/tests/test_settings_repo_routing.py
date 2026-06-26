"""Tests for /api/settings/repo-routing endpoints."""
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_preview_resolves(monkeypatch):
    from app.api import settings as st
    monkeypatch.setattr(st, "get_repo_routing", lambda: {"android": {"bands": [
        {"min_version": "4.0.0", "family": "native", "wrapper": "/tmp", "sub": "",
         "github_repo": "Plaud-AI/plaud-native-android", "symbol_profile": "native_android"}]}})
    from app.services import repo_router as rr
    monkeypatch.setattr(rr.os.path, "exists", lambda p: True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/settings/repo-routing/preview", json={"platform": "android", "version": "4.2.0"})
    assert r.status_code == 200
    assert r.json()["family"] == "native"
