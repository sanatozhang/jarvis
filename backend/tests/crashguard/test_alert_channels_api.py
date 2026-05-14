"""Unit tests for /api/crash/alert-channels endpoint.

直接调用 alert_channels_status() async 函数验证返回结构，
避免需要搭 FastAPI test client（太重）。
"""
from __future__ import annotations

import json
import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch


def _make_fake_session(rows):
    """构造一个 fake async context manager，simulate get_session() yield session。"""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = rows

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _fake_get_session():
        yield mock_session

    return _fake_get_session


def _make_rows():
    # 一条含 new / new_version 命中的行
    row_with_payload = MagicMock()
    row_with_payload.alert_payload = json.dumps({
        "new": [{"issue_id": "abc"}],
        "surge": [],
        "new_version": [{"issue_id": "xyz"}, {"issue_id": "uvw"}],
        "new_crash": [],
    })

    # 空 row（alert_payload = None）——测试容错路径
    row_empty = MagicMock()
    row_empty.alert_payload = None

    return [row_with_payload, row_empty]


@pytest.mark.asyncio
async def test_alert_channels_structure():
    """返回 dict 含 channels list，长度 4，每个 channel 有必要字段。"""
    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    fake_session = _make_fake_session(_make_rows())

    with patch("app.db.database.get_session", fake_session):
        from app.crashguard.api.crash import alert_channels_status
        result = await alert_channels_status()

    assert result["ok"] is True
    assert result["window_hours"] == 24
    assert "as_of" in result
    assert "channels" in result
    assert len(result["channels"]) == 4

    required_keys = {"name", "label", "count_24h", "enabled", "shadow_mode"}
    for ch in result["channels"]:
        missing = required_keys - set(ch.keys())
        assert not missing, f"channel {ch.get('name')} missing keys: {missing}"


@pytest.mark.asyncio
async def test_alert_channels_counts():
    """24h 命中数正确累计：new=1, surge=0, new_version=2, new_crash=0。"""
    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    fake_session = _make_fake_session(_make_rows())

    with patch("app.db.database.get_session", fake_session):
        from app.crashguard.api.crash import alert_channels_status
        result = await alert_channels_status()

    ch_by_name = {c["name"]: c for c in result["channels"]}
    assert ch_by_name["new"]["count_24h"] == 1
    assert ch_by_name["surge"]["count_24h"] == 0
    assert ch_by_name["new_version"]["count_24h"] == 2
    assert ch_by_name["new_crash"]["count_24h"] == 0

    # audit_rows_24h 应等于 mock 返回的行数（2 行，含 1 条空 payload）
    assert result["audit_rows_24h"] == 2


@pytest.mark.asyncio
async def test_alert_channels_datadog_cache_field():
    """datadog_cache 字段存在且包含 count 和 keys。"""
    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    fake_session = _make_fake_session(_make_rows())

    with patch("app.db.database.get_session", fake_session):
        from app.crashguard.api.crash import alert_channels_status
        result = await alert_channels_status()

    assert "datadog_cache" in result
    dc = result["datadog_cache"]
    assert "count" in dc
    assert "keys" in dc
