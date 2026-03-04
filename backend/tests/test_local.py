"""Tests for /api/local endpoints."""
from tests.conftest import seed_issue, seed_task, seed_analysis


async def test_list_in_progress(client, db_session):
    await seed_issue(db_session, "issue_ip", status="analyzing")
    resp = await client.get("/api/local/in-progress")
    assert resp.status_code == 200
    assert "issues" in resp.json()


async def test_list_completed(client, db_session):
    await seed_issue(db_session, "issue_done", status="done")
    resp = await client.get("/api/local/completed")
    assert resp.status_code == 200
    assert "issues" in resp.json()


async def test_list_failed(client, db_session):
    await seed_issue(db_session, "issue_fail", status="failed")
    resp = await client.get("/api/local/failed")
    assert resp.status_code == 200


async def test_tracking_with_filters(client, db_session):
    await seed_issue(db_session, "issue_track", status="done", platform="APP", source="local")
    resp = await client.get("/api/local/tracking", params={"platform": "APP", "source": "local"})
    assert resp.status_code == 200
    assert "total_pages" in resp.json()


async def test_tracking_pagination(client, db_session):
    for i in range(5):
        await seed_issue(db_session, f"pg_{i}", status="done")
    resp = await client.get("/api/local/tracking", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["page"] == 1
    assert data["page_size"] == 2


async def test_issue_detail(client, db_session):
    await seed_issue(db_session, "det_1", status="done")
    await seed_task(db_session, "task_det", "det_1")
    await seed_analysis(db_session, "task_det", "det_1")
    resp = await client.get("/api/local/det_1/detail")
    assert resp.status_code == 200


async def test_issue_detail_not_found(client):
    resp = await client.get("/api/local/nonexistent/detail")
    assert resp.status_code == 404


async def test_issue_analyses(client, db_session):
    await seed_issue(db_session, "ana_1")
    await seed_analysis(db_session, "task_a1", "ana_1")
    resp = await client.get("/api/local/ana_1/analyses")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_mark_inaccurate(client, db_session):
    await seed_issue(db_session, "inacc_1", status="done")
    resp = await client.post("/api/local/inacc_1/inaccurate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_mark_inaccurate_not_found(client):
    resp = await client.post("/api/local/missing/inaccurate")
    assert resp.status_code == 404


async def test_list_inaccurate(client, db_session):
    await seed_issue(db_session, "inacc_list", status="inaccurate")
    resp = await client.get("/api/local/inaccurate")
    assert resp.status_code == 200


async def test_soft_delete(client, db_session):
    await seed_issue(db_session, "del_1", status="done")
    resp = await client.delete("/api/local/del_1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


async def test_soft_delete_not_found(client):
    resp = await client.delete("/api/local/no_such")
    assert resp.status_code == 404
