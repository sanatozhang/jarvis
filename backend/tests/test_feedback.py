"""Tests for /api/feedback endpoints."""
from unittest.mock import patch, AsyncMock


async def test_submit_feedback(client):
    with patch("app.api.tasks._run_task", new_callable=AsyncMock):
        resp = await client.post("/api/feedback", data={
            "description": "蓝牙断连问题",
            "category": "蓝牙",
            "platform": "APP",
            "username": "testuser",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["record_id"].startswith("fb_")


async def test_submit_feedback_missing_description(client):
    resp = await client.post("/api/feedback", data={})
    assert resp.status_code == 422
