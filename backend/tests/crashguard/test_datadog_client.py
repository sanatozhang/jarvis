"""DatadogClient 测试"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.asyncio
async def test_list_issues_single_page(monkeypatch):
    """单页响应：返回所有 issue"""
    from app.crashguard.services.datadog_client import DatadogClient

    page = _load_fixture("datadog_issues_page2.json")  # meta.page 为空 = 末页

    async def fake_get(self, url, **kw):
        return httpx.Response(200, json=page)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    assert issues[0]["id"] == "def456"
    assert issues[0]["attributes"]["platform"] == "ios"


@pytest.mark.asyncio
async def test_list_issues_paginates(monkeypatch):
    """多页响应：跨页拼接"""
    from app.crashguard.services.datadog_client import DatadogClient

    pages = [
        _load_fixture("datadog_issues_page1.json"),
        _load_fixture("datadog_issues_page2.json"),
    ]
    call_count = {"n": 0}

    async def fake_get(self, url, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        return httpx.Response(200, json=pages[idx])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 2
    assert issues[0]["id"] == "abc123"
    assert issues[1]["id"] == "def456"


@pytest.mark.asyncio
async def test_list_issues_retries_on_5xx(monkeypatch):
    """5xx 错误重试 3 次后成功"""
    from app.crashguard.services.datadog_client import DatadogClient

    page = _load_fixture("datadog_issues_page2.json")
    call_count = {"n": 0}

    async def fake_get(self, url, **kw):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=page)

    async def fake_sleep(s):
        return  # 跳过真实等待

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")
    issues = await client.list_issues(window_hours=24)
    assert len(issues) == 1
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_rate_limit_circuit_breaker(monkeypatch):
    """10 分钟内 5 次 429 → 熔断 30 分钟"""
    from app.crashguard.services.datadog_client import (
        DatadogClient,
        DatadogRateLimitError,
        CircuitBreakerOpen,
    )

    async def fake_get(self, url, **kw):
        return httpx.Response(429, headers={"retry-after": "5"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    client = DatadogClient(api_key="k", app_key="a", site="datadoghq.com")

    # 前 5 次都应抛 DatadogRateLimitError
    for _ in range(5):
        with pytest.raises(DatadogRateLimitError):
            await client.list_issues(window_hours=24)

    # 第 6 次应抛熔断
    with pytest.raises(CircuitBreakerOpen):
        await client.list_issues(window_hours=24)
