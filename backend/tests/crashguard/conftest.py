"""Crashguard 测试 fixtures：每个测试自动清空 DatadogClient 模块级缓存，避免跨测试污染。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_datadog_list_cache():
    """C 路线引入了 5min 进程内缓存（list_issues_for_window）— 测试间共享同一进程会导致
    后跑的测试命中前面 mock 留下的结果。每个测试前后都清干净。
    """
    from app.crashguard.services import datadog_client as dd
    dd._list_cache.clear()
    yield
    dd._list_cache.clear()
