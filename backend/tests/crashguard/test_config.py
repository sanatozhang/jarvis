"""crashguard 配置加载测试"""
from __future__ import annotations

import os

import pytest


def test_settings_loads_defaults(monkeypatch):
    """无 env 时使用 yaml 默认值"""
    monkeypatch.delenv("CRASHGUARD_DATADOG_API_KEY", raising=False)
    monkeypatch.delenv("CRASHGUARD_ENABLED", raising=False)

    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    s = get_crashguard_settings()
    assert s.enabled is True
    assert s.pr_enabled is True
    assert s.feishu_enabled is True
    assert s.max_top_n == 20
    assert s.surge_multiplier == 1.5
    assert s.surge_min_events == 10
    assert s.regression_silent_versions == 3
    assert s.feasibility_pr_threshold == 0.7


def test_env_overrides_yaml(monkeypatch):
    """env 变量覆盖 yaml"""
    monkeypatch.setenv("CRASHGUARD_DATADOG_API_KEY", "test-key")
    monkeypatch.setenv("CRASHGUARD_ENABLED", "false")

    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    s = get_crashguard_settings()
    assert s.datadog_api_key == "test-key"
    assert s.enabled is False
