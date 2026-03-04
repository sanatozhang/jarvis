"""Tests for /api/eval endpoints."""
from unittest.mock import patch, AsyncMock


async def test_create_dataset(client):
    resp = await client.post("/api/eval/datasets", json={
        "name": "test-dataset", "description": "Test", "sample_ids": [],
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-dataset"
    assert "id" in resp.json()


async def test_list_datasets(client):
    await client.post("/api/eval/datasets", json={"name": "ds1"})
    resp = await client.get("/api/eval/datasets")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_dataset_not_found(client):
    resp = await client.get("/api/eval/datasets/999")
    assert resp.status_code == 404


async def test_start_run_no_dataset(client):
    resp = await client.post("/api/eval/run", json={"dataset_id": 999})
    assert resp.status_code == 404


async def test_start_run(client):
    ds_resp = await client.post("/api/eval/datasets", json={"name": "run-ds"})
    ds_id = ds_resp.json()["id"]
    with patch("app.services.eval_runner.run_eval", new_callable=AsyncMock):
        resp = await client.post("/api/eval/run", json={"dataset_id": ds_id})
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


async def test_list_runs(client):
    resp = await client.get("/api/eval/runs")
    assert resp.status_code == 200


async def test_get_run_not_found(client):
    resp = await client.get("/api/eval/runs/999")
    assert resp.status_code == 404
