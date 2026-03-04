"""Tests for /api/settings endpoints."""


async def test_get_agent_config(client):
    resp = await client.get("/api/settings/agent")
    assert resp.status_code == 200
    data = resp.json()
    assert "default" in data
    assert "timeout" in data
    assert "providers" in data


async def test_update_agent_config(client):
    resp = await client.put("/api/settings/agent", json={"timeout": 600})
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


async def test_get_concurrency_config(client):
    resp = await client.get("/api/settings/concurrency")
    assert resp.status_code == 200
    data = resp.json()
    assert "max_workers" in data
    assert "task_timeout" in data
