import pytest
import time
from app.crashguard.services.datadog_cache import DatadogCache


@pytest.fixture(autouse=True)
def _clear_cache():
    DatadogCache.clear()
    yield
    DatadogCache.clear()


@pytest.mark.asyncio
async def test_cache_miss_calls_fetch():
    calls = []
    async def fetch():
        calls.append(1)
        return {"data": "v1"}
    result = await DatadogCache.get_or_fetch("k1", ttl_seconds=10, fetch_fn=fetch)
    assert result == {"data": "v1"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cache_hit_skips_fetch():
    calls = []
    async def fetch():
        calls.append(1)
        return {"data": "v1"}
    await DatadogCache.get_or_fetch("k2", ttl_seconds=10, fetch_fn=fetch)
    await DatadogCache.get_or_fetch("k2", ttl_seconds=10, fetch_fn=fetch)
    await DatadogCache.get_or_fetch("k2", ttl_seconds=10, fetch_fn=fetch)
    assert len(calls) == 1   # 只调一次


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("app.crashguard.services.datadog_cache.time.time",
                        lambda: fake_now[0])
    calls = []
    async def fetch():
        calls.append(1)
        return {"i": len(calls)}
    await DatadogCache.get_or_fetch("k3", ttl_seconds=5, fetch_fn=fetch)
    fake_now[0] = 1006.0   # 跳到 6 秒后，过期
    result = await DatadogCache.get_or_fetch("k3", ttl_seconds=5, fetch_fn=fetch)
    assert result == {"i": 2}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_isolated_by_key():
    async def fetch_a():
        return "A"
    async def fetch_b():
        return "B"
    a = await DatadogCache.get_or_fetch("ka", ttl_seconds=10, fetch_fn=fetch_a)
    b = await DatadogCache.get_or_fetch("kb", ttl_seconds=10, fetch_fn=fetch_b)
    assert a == "A"
    assert b == "B"
