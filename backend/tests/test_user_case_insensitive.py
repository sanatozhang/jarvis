"""Username case-insensitivity at the DB layer (root cause of "建群人没进群").

Both login paths normalize usernames to lowercase before storing
(`users.py` does `.strip().lower()`, `auth.py` derives via
`derive_username_from_email` which lowercases). So every row's PK is lowercase.

But the escalate flow resolves the creator's email via
`db.get_user(body.escalated_by)`, where `escalated_by` is the frontend's
*display-case* username ("WM", "Akihiro", "Freya"). `get_user` did an
exact-match PK lookup → miss → empty email → the creator was the only group
member resolved via username→email, so only the creator was dropped from the
Feishu escalation group while oncall/fixed members (resolved straight from
config emails) got in.

Fix: the read path must honor the same lowercase normalization the write path
enforces.
"""
import pytest

from app.db import database as db


@pytest.mark.asyncio
async def test_get_user_is_case_insensitive(client):
    # Stored lowercase, exactly as the login flow writes it.
    await db.upsert_user("wm", feishu_email="wei.ming@plaud.ai", role="user")

    # Escalate sends the display-case username; must still resolve the email.
    user = await db.get_user("WM")
    assert user is not None
    assert user["feishu_email"] == "wei.ming@plaud.ai"


@pytest.mark.asyncio
async def test_get_user_handles_surrounding_whitespace(client):
    await db.upsert_user("akihiro", feishu_email="akihiro.minakawa@plaud.ai")
    user = await db.get_user("  Akihiro ")
    assert user is not None
    assert user["feishu_email"] == "akihiro.minakawa@plaud.ai"


@pytest.mark.asyncio
async def test_upsert_normalizes_so_no_duplicate_row(client):
    """upsert with display-case must update the lowercase row, not create a twin."""
    await db.upsert_user("freya", feishu_email="freya.ku@plaud.ai")
    await db.upsert_user("Freya", feishu_email="freya.ku@plaud.ai")

    all_users = await db.list_users()
    freyas = [u for u in all_users if u["username"] == "freya"]
    assert len(freyas) == 1
    assert not any(u["username"] == "Freya" for u in all_users)


@pytest.mark.asyncio
async def test_get_or_create_user_finds_existing_via_display_case(client):
    await db.upsert_user("venus", feishu_email="venus.li@plaud.ai")
    user = await db.get_or_create_user("Venus")
    assert user["feishu_email"] == "venus.li@plaud.ai"


@pytest.mark.asyncio
async def test_update_feishu_email_via_display_case(client):
    await db.upsert_user("ocean", feishu_email="")
    updated = await db.update_user_feishu_email("Ocean", "ocean.liu@plaud.ai")
    assert updated is not None
    assert updated["feishu_email"] == "ocean.liu@plaud.ai"
