"""Tests for /api/analytics endpoints."""
from unittest.mock import patch, AsyncMock


async def test_track_event(client):
    resp = await client.post("/api/analytics/track", json={
        "event_type": "page_visit", "username": "testuser", "detail": {"page": "/"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_dashboard(client):
    resp = await client.get("/api/analytics/dashboard", params={"days": 7})
    assert resp.status_code == 200
    assert "value_metrics" in resp.json()


async def test_rule_accuracy(client):
    with patch("app.services.rule_accuracy.get_rule_accuracy_stats", new_callable=AsyncMock, return_value={"rules": [], "total": 0}):
        resp = await client.get("/api/analytics/rule-accuracy")
    assert resp.status_code == 200
