"""DatadogClient 测试（mock SDK 调用层）"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import List, Optional

import pytest

from datadog_api_client.exceptions import ApiException


def _make_response(items: List[dict]) -> SimpleNamespace:
    """构造一个模拟 IssuesSearchResponse:
    - data: list of search_result（attributes=metric, relationships.issue.data.id 指向 issue）
    - included: list of Issue 对象
    """
    data = []
    included = []
    for it in items:
        issue_id = it["id"]
        data.append(SimpleNamespace(
            id=f"sr-{issue_id}",
            type="error_tracking_search_result",
            attributes=SimpleNamespace(
                total_count=it.get("events_count", 0),
                impacted_users=it.get("users_affected", 0),
                impacted_sessions=it.get("impacted_sessions", 0),
            ),
            relationships=SimpleNamespace(
                issue=SimpleNamespace(data=SimpleNamespace(id=issue_id))
            ),
        ))
        included.append(SimpleNamespace(
            id=issue_id,
            type="issue",
            attributes=SimpleNamespace(
                error_type=it.get("error_type", ""),
                error_message=it.get("error_message", ""),
                file_path=it.get("file_path", ""),
                function_name=it.get("function_name", ""),
                first_seen=it.get("first_seen", 0),
                last_seen=it.get("last_seen", 0),
                first_seen_version=it.get("first_seen_version", ""),
                last_seen_version=it.get("last_seen_version", ""),
                platform=it.get("platform", ""),
                service=it.get("service", ""),
            ),
        ))
    return SimpleNamespace(data=data, included=included)


def _make_api_exception(status: int, reason: str = "") -> ApiException:
    e = ApiException(status=status, reason=reason)
    return e


@pytest.mark.asyncio
async def test_list_issues_returns_normalized_dicts(monkeypatch):
    """单次 search 调用：返回展平后的 dict（id + attributes 平铺）"""
    from app.crashguard.services.datadog_client import DatadogClient

    response = _make_response([{
        "id": "abc123",
        "events_count": 145,
        "users_affected": 23,
        "error_type": "NullPointerException",
        "error_message": "null reference at AudioPlayer",
        "function_name": "AudioPlayer.play",
        "file_path": "lib/audio/player.dart",
        "first_seen": 1714003200000,
        "last_seen": 1714176000000,
        "first_seen_version": "1.4.7",
        "last_seen_version": "1.4.7",
        "platform": "flutter",
        "service": "plaud_ai",
    }])

    def fake_sync(self, body):
        return response

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    raw = issues[0]
    assert raw["id"] == "abc123"
    assert raw["type"] == "error_tracking_issue"
    attrs = raw["attributes"]
    assert attrs["title"] == "NullPointerException @ AudioPlayer.play"
    assert attrs["service"] == "plaud_ai"
    assert attrs["platform"] == "flutter"
    assert attrs["events_count"] == 145
    assert attrs["users_affected"] == 23
    assert attrs["first_seen_version"] == "1.4.7"
    assert attrs["first_seen_timestamp"] == 1714003200000
    assert "AudioPlayer.play" in attrs["stack_trace"]


@pytest.mark.asyncio
async def test_list_issues_handles_multiple_results(monkeypatch):
    """多个 search_result 都正确合并 included"""
    from app.crashguard.services.datadog_client import DatadogClient

    response = _make_response([
        {"id": "abc123", "events_count": 100, "error_type": "NPE", "platform": "flutter", "service": "plaud_ai"},
        {"id": "def456", "events_count": 50, "error_type": "OOM", "platform": "ios", "service": "plaud_ai"},
    ])

    def fake_sync(self, body):
        return response

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert [i["id"] for i in issues] == ["abc123", "def456"]
    assert issues[0]["attributes"]["events_count"] == 100
    assert issues[1]["attributes"]["platform"] == "ios"


@pytest.mark.asyncio
async def test_list_issues_retries_on_5xx(monkeypatch):
    """5xx 重试 3 次后成功"""
    from app.crashguard.services.datadog_client import DatadogClient
    from app.crashguard.services import datadog_client as dd

    call_count = {"n": 0}
    final_response = _make_response([{
        "id": "ok",
        "events_count": 1,
        "platform": "flutter",
        "service": "plaud_ai",
    }])

    def fake_sync(self, body):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise dd._RetryableSDKError(_make_api_exception(503, "Service Unavailable"))
        return final_response

    async def fake_sleep(_):
        return

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_rate_limit_circuit_breaker(monkeypatch):
    """5 次 429 → 第 6 次抛 CircuitBreakerOpen"""
    from app.crashguard.services.datadog_client import (
        DatadogClient,
        DatadogRateLimitError,
        CircuitBreakerOpen,
    )

    def fake_sync(self, body):
        self._record_rate_limit_event()
        raise DatadogRateLimitError("429")

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")

    for _ in range(5):
        with pytest.raises(DatadogRateLimitError):
            await client.list_issues(window_hours=24)

    with pytest.raises(CircuitBreakerOpen):
        await client.list_issues(window_hours=24)


@pytest.mark.asyncio
async def test_list_issues_propagates_unhandled_status(monkeypatch):
    """非 429 / 非 5xx 错误（例如 401 鉴权）不重试，直接抛"""
    from app.crashguard.services.datadog_client import DatadogClient

    def fake_sync(self, body):
        raise _make_api_exception(401, "Unauthorized")

    async def fake_sleep(_):
        return

    monkeypatch.setattr(DatadogClient, "_sync_search", fake_sync)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    with pytest.raises(ApiException) as exc_info:
        await client.list_issues(window_hours=24)
    assert exc_info.value.status == 401


def test_normalize_issue_payload():
    """Datadog 原始响应 → 内部统一结构"""
    from app.crashguard.services.datadog_client import normalize_issue
    raw = {
        "id": "abc123",
        "type": "error_tracking_issue",
        "attributes": {
            "title": "NullPointerException @ play",
            "service": "plaud_ai",
            "platform": "flutter",
            "first_seen_timestamp": 1714003200000,
            "last_seen_timestamp": 1714176000000,
            "first_seen_version": "1.4.7",
            "last_seen_version": "1.4.7",
            "events_count": 145,
            "users_affected": 23,
            "stack_trace": "NullPointerException\n  at A.x\n  at B.y",
            "tags": {"env": "prod"},
        },
    }
    norm = normalize_issue(raw)
    assert norm["datadog_issue_id"] == "abc123"
    assert norm["title"] == "NullPointerException @ play"
    assert norm["platform"] == "flutter"
    assert norm["service"] == "plaud_ai"
    assert norm["events_count"] == 145
    assert norm["users_affected"] == 23
    assert norm["first_seen_version"] == "1.4.7"
    assert norm["stack_trace"].startswith("NullPointerException")
    assert norm["tags"] == {"env": "prod"}
    assert norm["first_seen_at"].year == 2024  # 2024-04-25 unix ms


def test_normalize_handles_missing_fields():
    """缺失字段不报错，给默认值"""
    from app.crashguard.services.datadog_client import normalize_issue
    raw = {"id": "xxx", "attributes": {}}
    norm = normalize_issue(raw)
    assert norm["datadog_issue_id"] == "xxx"
    assert norm["title"] == ""
    assert norm["platform"] == ""
    assert norm["events_count"] == 0
