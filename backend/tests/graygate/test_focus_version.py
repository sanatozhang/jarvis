"""focus_version.py 单测——mock app.db.database，不碰真实 DB。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.graygate.services import focus_version as fv


@pytest.mark.asyncio
async def test_set_focus_version_writes_namespaced_key():
    with patch.object(fv.db, "set_oncall_config", new=AsyncMock()) as mock_set:
        await fv.set_focus_version("ios", "4.0.302-1050")
    mock_set.assert_awaited_once_with("graygate_focus_version_ios", "4.0.302-1050")


@pytest.mark.asyncio
async def test_get_focus_version_returns_none_when_unset():
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="")):
        result = await fv.get_focus_version("android")
    assert result is None


@pytest.mark.asyncio
async def test_get_focus_version_returns_stored_value():
    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(return_value="4.0.302-2010")):
        result = await fv.get_focus_version("android")
    assert result == "4.0.302-2010"


@pytest.mark.asyncio
async def test_clear_focus_version_writes_empty_string():
    with patch.object(fv.db, "set_oncall_config", new=AsyncMock()) as mock_set:
        await fv.clear_focus_version("ios")
    mock_set.assert_awaited_once_with("graygate_focus_version_ios", "")


@pytest.mark.asyncio
async def test_get_all_focus_versions_returns_both_platforms():
    async def fake_get(key, default=""):
        return {"graygate_focus_version_ios": "4.0.302-1050"}.get(key, default)

    with patch.object(fv.db, "get_oncall_config", new=AsyncMock(side_effect=fake_get)):
        result = await fv.get_all_focus_versions()
    assert result == {"ios": "4.0.302-1050", "android": None}
