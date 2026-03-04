"""Tests for /api/tasks endpoints."""
from unittest.mock import patch, AsyncMock
from tests.conftest import seed_issue, seed_task, seed_analysis


async def test_create_task(client, db_session):
    await seed_issue(db_session, "issue_ct", status="pending")
    with patch("app.api.tasks.run_analysis_pipeline", new_callable=AsyncMock):
        resp = await client.post("/api/tasks", json={"issue_id": "issue_ct", "username": "testuser"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["issue_id"] == "issue_ct"
    assert data["status"] == "queued"
    assert "task_id" in data


async def test_get_task_status(client, db_session):
    await seed_issue(db_session, "issue_gs")
    await seed_task(db_session, "task_gs", "issue_gs", status="done", progress=100)
    resp = await client.get("/api/tasks/task_gs")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


async def test_get_task_not_found(client):
    resp = await client.get("/api/tasks/nope")
    assert resp.status_code == 404


async def test_get_task_result(client, db_session):
    await seed_issue(db_session, "issue_gr")
    await seed_task(db_session, "task_gr", "issue_gr")
    await seed_analysis(db_session, "task_gr", "issue_gr")
    resp = await client.get("/api/tasks/task_gr/result")
    assert resp.status_code == 200
    assert resp.json()["problem_type"] == "蓝牙连接"


async def test_get_task_result_no_analysis(client, db_session):
    await seed_task(db_session, "task_na", "no_issue")
    resp = await client.get("/api/tasks/task_na/result")
    assert resp.status_code == 404


async def test_list_tasks(client, db_session):
    await seed_task(db_session, "task_l1", "issue_l1")
    await seed_task(db_session, "task_l2", "issue_l2")
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_batch_analyze(client, db_session):
    await seed_issue(db_session, "b1", status="pending")
    await seed_issue(db_session, "b2", status="pending")
    with patch("app.api.tasks.run_analysis_pipeline", new_callable=AsyncMock):
        resp = await client.post("/api/tasks/batch", json={"issue_ids": ["b1", "b2"]})
    assert resp.status_code == 200
    assert len(resp.json()) == 2
