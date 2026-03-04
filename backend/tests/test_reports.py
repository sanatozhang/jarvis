"""Tests for /api/reports endpoints."""


async def test_daily_report_empty(client):
    resp = await client.get("/api/reports/daily/2026-01-01")
    assert resp.status_code == 200
    assert resp.json()["total_issues"] == 0


async def test_daily_report_invalid_date(client):
    resp = await client.get("/api/reports/daily/not-a-date")
    assert resp.status_code == 400


async def test_daily_report_markdown(client):
    resp = await client.get("/api/reports/daily/2026-01-01/markdown")
    assert resp.status_code == 200


async def test_report_dates(client):
    resp = await client.get("/api/reports/dates")
    assert resp.status_code == 200
    assert "dates" in resp.json()
