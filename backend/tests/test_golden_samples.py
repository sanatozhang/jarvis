"""Tests for /api/golden-samples endpoints."""
from unittest.mock import patch, AsyncMock


async def test_list_samples_empty(client):
    resp = await client.get("/api/golden-samples")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_stats(client):
    resp = await client.get("/api/golden-samples/stats")
    assert resp.status_code == 200


async def test_promote_sample_not_found(client):
    with patch("app.api.golden_samples.promote_analysis_to_sample", new_callable=AsyncMock, side_effect=ValueError("Not found")):
        resp = await client.post("/api/golden-samples", json={"analysis_id": 999})
    assert resp.status_code == 404


async def test_delete_sample_not_found(client):
    resp = await client.delete("/api/golden-samples/999")
    assert resp.status_code == 404
