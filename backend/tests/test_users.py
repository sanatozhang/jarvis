"""Tests for /api/users endpoints."""


async def test_login_creates_user(client):
    resp = await client.post("/api/users/login", json={"username": "newuser"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "newuser"
    assert resp.json()["role"] in ("user", "admin")


async def test_login_empty_username(client):
    resp = await client.post("/api/users/login", json={"username": ""})
    assert resp.status_code == 400


async def test_login_idempotent(client):
    await client.post("/api/users/login", json={"username": "alice"})
    resp = await client.post("/api/users/login", json={"username": "alice"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


async def test_get_user(client):
    await client.post("/api/users/login", json={"username": "bob"})
    resp = await client.get("/api/users/bob")
    assert resp.status_code == 200
    assert resp.json()["username"] == "bob"


async def test_get_user_not_found(client):
    resp = await client.get("/api/users/nonexistent")
    assert resp.status_code == 404


async def test_list_users(client):
    await client.post("/api/users/login", json={"username": "u1"})
    await client.post("/api/users/login", json={"username": "u2"})
    resp = await client.get("/api/users")
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()]
    assert "u1" in usernames
    assert "u2" in usernames
