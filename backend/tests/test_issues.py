"""Tests for /api/issues endpoints (Feishu mocked)."""
from unittest.mock import patch, AsyncMock, MagicMock
from app.models.schemas import Issue


async def test_list_pending_issues(client):
    mock_issues = [
        Issue(record_id="r1", description="test issue 1"),
        Issue(record_id="r2", description="test issue 2"),
    ]
    with patch("app.api.issues.FeishuClient") as mock_cls:
        mock_cls.return_value.list_pending_issues = AsyncMock(return_value=mock_issues)
        resp = await client.get("/api/issues")
    assert resp.status_code == 200
    assert "issues" in resp.json()


async def test_refresh_issues(client):
    with patch("app.api.issues.FeishuClient") as mock_cls:
        mock_cls.invalidate_cache = MagicMock()
        resp = await client.post("/api/issues/refresh")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cache_invalidated"


async def test_get_single_issue(client):
    mock_issue = Issue(record_id="r1", description="test")
    with patch("app.api.issues.FeishuClient") as mock_cls:
        mock_cls.return_value.get_issue = AsyncMock(return_value=mock_issue)
        resp = await client.get("/api/issues/r1")
    assert resp.status_code == 200
