"""update_user_feishu_email behavior."""
import pytest

from app.db import database as db


@pytest.mark.asyncio
async def test_update_existing_user(client):
    await db.upsert_user("legacy", feishu_email="", role="user")
    result = await db.update_user_feishu_email("legacy", "legacy@plaud.ai")
    assert result is not None
    assert result["feishu_email"] == "legacy@plaud.ai"
    assert result["role"] == "user"  # role unchanged

    fresh = await db.get_user("legacy")
    assert fresh["feishu_email"] == "legacy@plaud.ai"


@pytest.mark.asyncio
async def test_returns_none_for_missing_user(client):
    result = await db.update_user_feishu_email("ghost", "ghost@plaud.ai")
    assert result is None


@pytest.mark.asyncio
async def test_does_not_change_role(client):
    await db.upsert_user("boss", feishu_email="", role="admin")
    await db.update_user_feishu_email("boss", "boss@plaud.ai")
    fresh = await db.get_user("boss")
    assert fresh["role"] == "admin"
