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


@pytest.fixture(autouse=True)
def _stub_claude_auth_probe(monkeypatch):
    """run_job_health_check 会顺带探测 claude 登录态——测试里绝不能真起 claude CLI
    （慢、耗额度，本机登录态还会让结果不稳定）。默认桩成"探测成功"并清节流状态；
    专测探测逻辑的用例自行再 monkeypatch 覆盖。
    """
    from app.crashguard.services import claude_auth_probe as cap

    async def _ok(timeout: float = 0):
        return True, "", "OK"

    monkeypatch.setattr(cap, "probe_claude_auth", _ok)
    monkeypatch.setattr(cap, "_last_probe_at", None)
    monkeypatch.setattr(cap, "_last_alerted_at", None)
    monkeypatch.setattr(cap, "_consecutive_failures", 0)
