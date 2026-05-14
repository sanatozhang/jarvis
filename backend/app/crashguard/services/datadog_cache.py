"""
进程内 TTL 缓存：降低 Datadog API 调用频率。

底层逻辑：jarvis 单实例 Docker 部署，dict + TTL 足够；不引入 Redis（YAGNI）。
进程重启缓存失效，首次 cron 自动回填，影响窗口 ≤3h。
"""
from __future__ import annotations
import time
from typing import Any, Awaitable, Callable


class DatadogCache:
    _cache: dict = {}
    _expires_at: dict = {}

    @classmethod
    async def get_or_fetch(
        cls,
        key: str,
        ttl_seconds: int,
        fetch_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """命中返回缓存，未命中/过期则调 fetch_fn 并缓存。"""
        now = time.time()
        if key in cls._cache and cls._expires_at.get(key, 0) > now:
            return cls._cache[key]
        data = await fetch_fn()
        cls._cache[key] = data
        cls._expires_at[key] = now + ttl_seconds
        return data

    @classmethod
    def clear(cls) -> None:
        """测试钩子：清空缓存。"""
        cls._cache.clear()
        cls._expires_at.clear()

    @classmethod
    def stats(cls) -> dict:
        """供 audit / 验证脚本用。"""
        return {"keys": list(cls._cache.keys()), "count": len(cls._cache)}
