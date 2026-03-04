"""Tests for /api/health endpoints."""
from unittest.mock import patch, MagicMock


async def test_health_check(client):
    """GET /api/health returns healthy status."""
    with patch("app.services.rule_engine.RuleEngine") as mock_cls:
        mock_cls.return_value.list_rules.return_value = []
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "degraded")
    assert "checks" in data


async def test_health_agents(client):
    """GET /api/health/agents returns agent availability."""
    with patch("shutil.which", return_value=None):
        resp = await client.get("/api/health/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert "claude_code" in data
    assert "codex" in data
