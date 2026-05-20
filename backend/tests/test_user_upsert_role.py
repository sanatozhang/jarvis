"""upsert_user with explicit role param."""
import pytest

from app.db import database as db


@pytest.mark.asyncio
async def test_upsert_creates_user_with_role(client):
    user = await db.upsert_user("alice", feishu_email="alice@plaud.ai", role="admin")
    assert user["role"] == "admin"
    assert user["feishu_email"] == "alice@plaud.ai"


@pytest.mark.asyncio
async def test_upsert_updates_existing_role(client):
    await db.upsert_user("bob", feishu_email="bob@plaud.ai", role="user")
    user = await db.upsert_user("bob", feishu_email="bob@plaud.ai", role="admin")
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_upsert_default_role_preserved_when_not_passed(client):
    """Backwards compat: role kwarg omitted → behavior unchanged."""
    user = await db.upsert_user("carol", feishu_email="carol@plaud.ai")
    assert user["role"] == "user"
