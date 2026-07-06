"""Tests for /api/health endpoints."""
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


async def test_health_check(client):
    """GET /api/health returns healthy status."""
    with patch("app.services.rule_engine.RuleEngine") as mock_cls:
        mock_cls.return_value.list_rules.return_value = []
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "degraded")
    assert "checks" in data


@pytest.mark.asyncio
async def test_health_overall_healthy_when_all_ok():
    """Regression: the agents check is a nested dict without its own top-level
    "status"; the overall aggregate must still resolve to "healthy" when every
    agent is ok (previously it read None for agents → always "degraded")."""
    import app.api.health as h
    agents_ok = {
        "claude_code": {"status": "ok", "available": True, "version": "x"},
        "codex": {"status": "ok", "available": True, "version": "y"},
    }
    with patch.object(h, "_detect_agents", new=AsyncMock(return_value=agents_ok)):
        r = await h.health_check()
    assert r["status"] == "healthy", r
    assert r["checks"]["agents"]["status"] == "ok"
    # children preserved
    assert "claude_code" in r["checks"]["agents"]


@pytest.mark.asyncio
async def test_health_degraded_when_agent_errors():
    """A genuinely failing agent must flip the overall status to degraded."""
    import app.api.health as h
    agents_bad = {
        "claude_code": {"status": "error", "error": "spawn failed"},
        "codex": {"status": "ok"},
    }
    with patch.object(h, "_detect_agents", new=AsyncMock(return_value=agents_bad)):
        r = await h.health_check()
    assert r["status"] == "degraded", r
    assert r["checks"]["agents"]["status"] == "degraded"


async def test_health_agents(client):
    """GET /api/health/agents returns agent availability."""
    with patch("shutil.which", return_value=None):
        resp = await client.get("/api/health/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert "claude_code" in data
    assert "codex" in data
