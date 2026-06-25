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


from datetime import date


def test_resolve_duty_week_current_week():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}, {"members": ["b@x.com"]}]
    # start 2026-06-01, today 2026-06-25 → 24 天 → week 3 → 3%2=1 → b 当周值周
    info = resolve_duty_week(groups, "2026-06-01", "B@x.com", date(2026, 6, 25))
    assert info is not None
    assert info["group_index"] == 1
    assert info["week_num"] == 3
    assert info["is_current"] is True
    assert info["week_start"] == date(2026, 6, 22)
    assert info["week_end"] == date(2026, 6, 28)
    assert info["partners"] == []


def test_resolve_duty_week_most_recent_past():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com", "c@x.com"]}, {"members": ["b@x.com"]}]
    # a 在 group 0；today week 3 → a 最近值周是 week 2（2026-06-15）
    info = resolve_duty_week(groups, "2026-06-01", "a@x.com", date(2026, 6, 25))
    assert info["group_index"] == 0
    assert info["week_num"] == 2
    assert info["is_current"] is False
    assert info["week_start"] == date(2026, 6, 15)
    assert info["partners"] == ["c@x.com"]


def test_resolve_duty_week_not_member():
    from app.api.oncall import resolve_duty_week
    groups = [{"members": ["a@x.com"]}]
    assert resolve_duty_week(groups, "2026-06-01", "nobody@x.com", date(2026, 6, 25)) is None
    assert resolve_duty_week([], "2026-06-01", "a@x.com", date(2026, 6, 25)) is None
    assert resolve_duty_week(groups, "", "a@x.com", date(2026, 6, 25)) is None
