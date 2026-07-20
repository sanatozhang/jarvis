"""DatadogClient.search_logs_page() 单测（2026-07-20）。

背景：jank_watchdog_block 是纯 Logs 事件，不经过 Error Tracking，官方 SDK 的
list_issues() 覆盖不到这条查询，跟 _scalar_user_cardinality 一样手写 urllib
调用 Logs Events Search API v2（POST /api/v2/logs/events/search），游标分页
（meta.page.after）。
"""
from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest


def _mock_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


@pytest.mark.asyncio
async def test_search_logs_page_returns_data_and_next_cursor():
    from app.crashguard.services.datadog_client import DatadogClient

    body = {
        "data": [{"id": "AQAAA1", "attributes": {"attributes": {"category": "performance"}}}],
        "meta": {"page": {"after": "cursor-abc"}},
    }
    with patch("urllib.request.urlopen", return_value=_mock_response(body)) as mock_urlopen:
        client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
        result = await client.search_logs_page(
            query="@category:performance jank_watchdog_block",
            from_ms=1000, to_ms=2000,
        )

    assert result["data"] == body["data"]
    assert result["next_cursor"] == "cursor-abc"

    req = mock_urlopen.call_args[0][0]
    assert req.headers["Dd-api-key"] == "k"
    assert req.headers["Dd-application-key"] == "a"
    sent_body = json.loads(req.data)
    assert "jank_watchdog_block" in sent_body["filter"]["query"]
    assert sent_body["filter"]["from"] == "1000"
    assert sent_body["filter"]["to"] == "2000"
    assert "cursor" not in sent_body["page"]


@pytest.mark.asyncio
async def test_search_logs_page_passes_cursor_when_provided():
    from app.crashguard.services.datadog_client import DatadogClient

    body = {"data": [], "meta": {"page": {}}}
    with patch("urllib.request.urlopen", return_value=_mock_response(body)) as mock_urlopen:
        client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
        result = await client.search_logs_page(
            query="@category:performance jank_watchdog_block",
            from_ms=1000, to_ms=2000, cursor="prev-cursor",
        )

    sent_body = json.loads(mock_urlopen.call_args[0][0].data)
    assert sent_body["page"]["cursor"] == "prev-cursor"
    assert result["data"] == []
    assert result["next_cursor"] is None  # meta.page 里没有 "after" → 没有下一页


@pytest.mark.asyncio
async def test_search_logs_page_injects_service_filter():
    """跟其余所有查询一样，要注入 app-only service_filter，避免混入 web/desktop 数据。"""
    from app.crashguard.services.datadog_client import DatadogClient

    body = {"data": [], "meta": {"page": {}}}
    with patch("urllib.request.urlopen", return_value=_mock_response(body)) as mock_urlopen:
        client = DatadogClient(
            api_key="k", app_key="a", site="datadoghq.com",
            service_filter="service:plaud_ios",
        )
        await client.search_logs_page(
            query="@category:performance jank_watchdog_block", from_ms=1000, to_ms=2000,
        )

    sent_body = json.loads(mock_urlopen.call_args[0][0].data)
    assert "service:plaud_ios" in sent_body["filter"]["query"]


@pytest.mark.asyncio
async def test_search_logs_page_raises_rate_limit_error_on_429():
    from app.crashguard.services.datadog_client import DatadogClient, DatadogRateLimitError

    http_error = urllib.error.HTTPError(
        url="http://x", code=429, msg="Too Many Requests", hdrs=None, fp=None,
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
        with pytest.raises(DatadogRateLimitError):
            await client.search_logs_page(
                query="@category:performance jank_watchdog_block", from_ms=1000, to_ms=2000,
            )


@pytest.mark.asyncio
async def test_search_logs_page_retries_once_on_5xx_then_succeeds():
    from app.crashguard.services.datadog_client import DatadogClient

    http_error = urllib.error.HTTPError(
        url="http://x", code=503, msg="Service Unavailable", hdrs=None, fp=None,
    )
    body = {"data": [], "meta": {"page": {}}}
    with patch(
        "urllib.request.urlopen",
        side_effect=[http_error, _mock_response(body)],
    ), patch("time.sleep"):
        client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
        result = await client.search_logs_page(
            query="@category:performance jank_watchdog_block", from_ms=1000, to_ms=2000,
        )
    assert result == {"data": [], "next_cursor": None}
