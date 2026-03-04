"""Tests for /api/env endpoints."""
from unittest.mock import patch
from tests.conftest import seed_admin, seed_user


async def test_get_env_admin(client):
    await seed_admin(client, "sanato")
    with patch("app.api.env_settings.ENV_PATH") as mock_path:
        mock_path.exists.return_value = False
        resp = await client.get("/api/env", params={"username": "sanato"})
    assert resp.status_code == 200
    assert "groups" in resp.json()


async def test_get_env_non_admin(client):
    await seed_user(client, "regular")
    resp = await client.get("/api/env", params={"username": "regular"})
    assert resp.status_code == 403


async def test_update_env_non_admin(client):
    await seed_user(client, "regular2")
    resp = await client.put("/api/env", params={"username": "regular2"}, json={
        "updates": {"FEISHU_APP_ID": "new_id"},
    })
    assert resp.status_code == 403
