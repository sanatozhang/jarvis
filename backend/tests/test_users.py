"""Tests for /api/users endpoints."""


async def test_login_creates_user(client):
    # 新用户**必须**带 @plaud.ai 邮箱（`login_or_register` 的 docstring 写了）。
    # 这几条用例原来不带 email，端点加上这道校验之后就一直是 400。
    resp = await client.post(
        "/api/users/login",
        json={"username": "newuser", "email": "newuser@plaud.ai"},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "newuser"
    assert resp.json()["role"] in ("user", "admin")


async def test_login_empty_username(client):
    resp = await client.post("/api/users/login", json={"username": ""})
    assert resp.status_code == 400


async def test_login_idempotent(client):
    await client.post("/api/users/login",
                      json={"username": "alice", "email": "alice@plaud.ai"})
    # 第二次**不带** email —— 已存在的用户不需要再给，这正是幂等的含义
    resp = await client.post("/api/users/login", json={"username": "alice"})
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice"


async def test_get_user(client):
    await client.post("/api/users/login",
                      json={"username": "bob", "email": "bob@plaud.ai"})
    resp = await client.get("/api/users/bob")
    assert resp.status_code == 200
    assert resp.json()["username"] == "bob"


async def test_get_user_not_found(client):
    resp = await client.get("/api/users/nonexistent")
    assert resp.status_code == 404


async def test_list_users(client):
    await client.post("/api/users/login", json={"username": "u1", "email": "u1@plaud.ai"})
    await client.post("/api/users/login", json={"username": "u2", "email": "u2@plaud.ai"})
    resp = await client.get("/api/users")
    assert resp.status_code == 200
    usernames = [u["username"] for u in resp.json()]
    assert "u1" in usernames
    assert "u2" in usernames
