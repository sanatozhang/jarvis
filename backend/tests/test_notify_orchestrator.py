"""notify_orchestrator behavior."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.notify_orchestrator import notify_users_by_username


@pytest.mark.asyncio
async def test_sends_to_users_with_feishu_email(client):
    from app.db import database as db
    await db.upsert_user("alice", feishu_email="alice@plaud.ai")
    await db.upsert_user("bob", feishu_email="bob@plaud.ai")

    with patch("app.services.notify_orchestrator.feishu_send_message",
                new_callable=AsyncMock) as mock_send:
        result = await notify_users_by_username(
            usernames=["alice", "bob"], text="hello",
        )

    assert mock_send.await_count == 2
    assert sorted(result.sent) == ["alice", "bob"]
    assert result.skipped == []
    assert result.failed == []


@pytest.mark.asyncio
async def test_skips_user_without_feishu_email(client):
    from app.db import database as db
    await db.upsert_user("noemail", feishu_email="")

    with patch("app.services.notify_orchestrator.feishu_send_message",
                new_callable=AsyncMock) as mock_send:
        result = await notify_users_by_username(usernames=["noemail"], text="hi")

    mock_send.assert_not_awaited()
    assert ("noemail", "no_feishu_email") in result.skipped


@pytest.mark.asyncio
async def test_skips_unknown_user(client):
    with patch("app.services.notify_orchestrator.feishu_send_message",
                new_callable=AsyncMock) as mock_send:
        result = await notify_users_by_username(usernames=["ghost"], text="hi")

    mock_send.assert_not_awaited()
    assert ("ghost", "user_not_found") in result.skipped


@pytest.mark.asyncio
async def test_collects_send_failures(client):
    from app.db import database as db
    await db.upsert_user("alice", feishu_email="alice@plaud.ai")

    with patch("app.services.notify_orchestrator.feishu_send_message",
                new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        result = await notify_users_by_username(usernames=["alice"], text="hi")

    assert result.sent == []
    assert result.failed == [("alice", "boom")]
