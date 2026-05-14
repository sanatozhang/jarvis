"""Unit tests for /api/crash/alert-feedback endpoint (Sprint E).

测试策略：mock DB session，直接调 async 函数，不需要 FastAPI test client。
"""
from __future__ import annotations

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


def _make_fake_session_with_row(row):
    """构造一个 fake async context manager，simulate get_session() yield session。"""
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = row

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    @asynccontextmanager
    async def _fake_get_session():
        yield mock_session

    return _fake_get_session, mock_session


@pytest.mark.asyncio
async def test_alert_feedback_endpoint_records_good():
    """GET /alert-feedback?alert_id=1&label=good → DB 行 feedback='good' feedback_at 非空。"""
    row = MagicMock()
    row.id = 1
    row.feedback = None
    row.feedback_at = None
    row.feedback_by = None

    fake_session, mock_session = _make_fake_session_with_row(row)

    with patch("app.db.database.get_session", fake_session):
        from app.crashguard.api.crash import record_alert_feedback
        response = await record_alert_feedback(alert_id=1, label="good", by="sanato@plaud.ai")

    # 验证 DB 行字段被赋值
    assert row.feedback == "good"
    assert row.feedback_at is not None
    assert row.feedback_by == "sanato@plaud.ai"
    # 验证 commit 被调用
    mock_session.commit.assert_called_once()
    # 验证返回 HTML，包含正确 emoji 和 label
    body = response.body.decode()
    assert "👍" in body
    assert "good" in body
    assert "alert #1" in body


@pytest.mark.asyncio
async def test_alert_feedback_endpoint_records_bad():
    """GET /alert-feedback?alert_id=2&label=bad → DB 行 feedback='bad'。"""
    row = MagicMock()
    row.id = 2
    row.feedback = None
    row.feedback_at = None
    row.feedback_by = None

    fake_session, mock_session = _make_fake_session_with_row(row)

    with patch("app.db.database.get_session", fake_session):
        from app.crashguard.api.crash import record_alert_feedback
        response = await record_alert_feedback(alert_id=2, label="bad", by="")

    assert row.feedback == "bad"
    assert row.feedback_at is not None
    assert row.feedback_by == ""
    body = response.body.decode()
    assert "👎" in body
    assert "bad" in body


@pytest.mark.asyncio
async def test_alert_feedback_invalid_label():
    """label='weird' → 返回 400 HTML 错误页，不写 DB。"""
    from app.crashguard.api.crash import record_alert_feedback

    # 不需要 patch session，因为非法 label 在进 DB 前就返回
    response = await record_alert_feedback(alert_id=1, label="weird")
    assert response.status_code == 400
    body = response.body.decode()
    assert "good" in body or "bad" in body


@pytest.mark.asyncio
async def test_alert_feedback_not_found():
    """alert_id 不存在 → 返回 404 HTML，不崩溃。"""
    fake_session, mock_session = _make_fake_session_with_row(None)

    with patch("app.db.database.get_session", fake_session):
        from app.crashguard.api.crash import record_alert_feedback
        response = await record_alert_feedback(alert_id=9999, label="good")

    assert response.status_code == 404
    body = response.body.decode()
    assert "9999" in body
    # 不应调用 commit
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_alert_channels_feedback_24h_field():
    """alert-channels 返回中有 feedback_24h 字段，且 good/bad 正确统计。"""
    import json

    # 第一行：有 good 反馈
    row_good = MagicMock()
    row_good.alert_payload = json.dumps({"new": [{"issue_id": "abc"}], "surge": [], "new_version": [], "new_crash": []})
    row_good.feedback = "good"

    # 第二行：有 bad 反馈
    row_bad = MagicMock()
    row_bad.alert_payload = json.dumps({"new": [], "surge": [], "new_version": [], "new_crash": []})
    row_bad.feedback = "bad"

    # 第三行：无反馈
    row_none = MagicMock()
    row_none.alert_payload = json.dumps({"new": [], "surge": [], "new_version": [], "new_crash": []})
    row_none.feedback = None

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [row_good, row_bad, row_none]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _fake_get_session():
        yield mock_session

    from app.crashguard.services.datadog_cache import DatadogCache
    DatadogCache.clear()

    with patch("app.db.database.get_session", _fake_get_session):
        from app.crashguard.api.crash import alert_channels_status
        result = await alert_channels_status()

    assert "feedback_24h" in result
    fb = result["feedback_24h"]
    assert fb["good"] == 1
    assert fb["bad"] == 1
    assert fb["total_with_feedback"] == 2
    assert fb["total_audit_rows"] == 3
