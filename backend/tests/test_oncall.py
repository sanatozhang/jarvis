"""Tests for /api/oncall endpoints."""
from tests.conftest import seed_admin, seed_user


async def test_get_current_oncall(client):
    resp = await client.get("/api/oncall/current")
    assert resp.status_code == 200
    assert "members" in resp.json()
    assert "count" in resp.json()


async def test_get_schedule(client):
    resp = await client.get("/api/oncall/schedule")
    assert resp.status_code == 200
    assert "groups" in resp.json()
    assert "total_groups" in resp.json()


async def test_update_schedule_admin(client):
    await seed_admin(client, "sanato")
    resp = await client.put("/api/oncall/schedule", params={"username": "sanato"}, json={
        "groups": [{"members": ["a@test.com", "b@test.com"]}],
        "start_date": "2026-03-01",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_update_schedule_non_admin(client):
    await seed_user(client, "regular")
    resp = await client.put("/api/oncall/schedule", params={"username": "regular"}, json={
        "groups": [{"members": ["a@test.com"]}],
        "start_date": "2026-03-01",
    })
    assert resp.status_code == 403
