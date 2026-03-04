"""Tests for /api/v1 public API endpoints."""
from unittest.mock import patch, AsyncMock
from tests.conftest import seed_task, seed_analysis


async def test_submit_analysis_no_auth(client):
    with patch("app.api.v1_analyze.API_KEY", ""):
        with patch("app.api.v1_analyze._run_api_analysis", new_callable=AsyncMock):
            resp = await client.post("/api/v1/analyze", data={"description": "Test issue"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "processing"
    assert "task_id" in data


async def test_submit_analysis_bad_key(client):
    with patch("app.api.v1_analyze.API_KEY", "secret123"):
        resp = await client.post("/api/v1/analyze",
            data={"description": "Test"},
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 403


async def test_poll_result_not_found(client):
    with patch("app.api.v1_analyze.API_KEY", ""):
        resp = await client.get("/api/v1/analyze/nonexistent")
    assert resp.status_code == 404


async def test_poll_result_done(client, db_session):
    await seed_task(db_session, "api_task", "api_issue", status="done")
    await seed_analysis(db_session, "api_task", "api_issue")
    with patch("app.api.v1_analyze.API_KEY", ""):
        resp = await client.get("/api/v1/analyze/api_task")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["problem_type"] == "蓝牙连接"
