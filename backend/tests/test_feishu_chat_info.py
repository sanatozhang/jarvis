"""Tests for get_chat_info read-only helper in feishu_cli."""

import pytest
from app.services import feishu_cli


@pytest.mark.asyncio
async def test_get_chat_info_happy_path(monkeypatch):
    """Happy path: _feishu_api returns data → get_chat_info extracts it."""
    async def fake_api(*args, **kwargs):
        return {"data": {"chat_id": "oc_x", "name": "APP Team"}}

    monkeypatch.setattr(feishu_cli, "_feishu_api", fake_api)
    result = await feishu_cli.get_chat_info("oc_x")
    assert result == {"chat_id": "oc_x", "name": "APP Team"}


@pytest.mark.asyncio
async def test_get_chat_info_empty_chat_id(monkeypatch):
    """Empty chat_id → returns {} without calling _feishu_api."""
    call_count = 0

    async def fake_api_should_not_be_called(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Should not be called")

    monkeypatch.setattr(feishu_cli, "_feishu_api", fake_api_should_not_be_called)
    result = await feishu_cli.get_chat_info("")
    assert result == {}
    assert call_count == 0  # Ensure _feishu_api was never called


@pytest.mark.asyncio
async def test_get_chat_info_api_error(monkeypatch):
    """_feishu_api raises → returns {} (non-fatal)."""
    async def fake_api_raises(*args, **kwargs):
        raise Exception("API error")

    monkeypatch.setattr(feishu_cli, "_feishu_api", fake_api_raises)
    result = await feishu_cli.get_chat_info("oc_x")
    assert result == {}
