"""Smoke test to verify test infrastructure works."""


async def test_app_starts(client):
    """Verify the test client can reach the app."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200
