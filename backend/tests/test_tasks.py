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


async def _start_events(db_session, issue_id: str):
    """读 analysis_start 事件（用于断言 followup_question 是否被记录，供失败后重放）。"""
    import json
    from sqlalchemy import select
    from app.db.database import EventRecord
    async with db_session() as s:
        rows = (await s.execute(
            select(EventRecord).where(
                EventRecord.event_type == "analysis_start",
                EventRecord.issue_id == issue_id,
            )
        )).scalars().all()
    return [json.loads(r.detail_json or "{}") for r in rows]


async def test_followup_task_records_question_in_start_event(client, db_session):
    # 追问：analysis_start 事件须带 followup_question + task_id，失败后可据此重放
    await seed_issue(db_session, "issue_fq", status="done")
    with patch("app.api.tasks.run_analysis_pipeline", new_callable=AsyncMock):
        resp = await client.post("/api/tasks", json={
            "issue_id": "issue_fq", "username": "wm",
            "followup_question": "看更早的蓝牙日志",
        })
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    details = await _start_events(db_session, "issue_fq")
    assert len(details) == 1
    assert details[0]["followup_question"] == "看更早的蓝牙日志"
    assert details[0]["task_id"] == task_id


async def test_base_task_start_event_has_no_followup(client, db_session):
    # 首次分析（无追问）：start 事件不应塞 followup_question，保持干净
    await seed_issue(db_session, "issue_base", status="pending")
    with patch("app.api.tasks.run_analysis_pipeline", new_callable=AsyncMock):
        resp = await client.post("/api/tasks", json={"issue_id": "issue_base", "username": "u"})
    assert resp.status_code == 200
    details = await _start_events(db_session, "issue_base")
    assert len(details) == 1
    assert "followup_question" not in details[0]
