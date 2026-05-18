"""crashguard 配置加载测试"""
from __future__ import annotations

import os

import pytest


def test_settings_loads_defaults(monkeypatch):
    """无 env 时使用 yaml 默认值"""
    monkeypatch.delenv("CRASHGUARD_DATADOG_API_KEY", raising=False)
    monkeypatch.delenv("CRASHGUARD_ENABLED", raising=False)
    # 防 .env 污染：pydantic_settings 直读 .env 文件，monkeypatch.delenv 不生效，
    # 显式 setenv 把"测期望的默认值"塞回去（env > .env > yaml > default 优先级）。
    monkeypatch.setenv("CRASHGUARD_FEISHU_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_PR_ENABLED", "true")
    monkeypatch.setenv("CRASHGUARD_ENABLED", "true")

    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    s = get_crashguard_settings()
    assert s.enabled is True
    assert s.pr_enabled is True
    assert s.feishu_enabled is True
    assert s.max_top_n == 20
    assert s.surge_multiplier == 1.5
    assert s.surge_min_events == 100
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


def test_datadog_split_queries_load_from_yaml(monkeypatch):
    """fatal / non_fatal 双路 query 应允许通过 config.yaml 覆盖。"""
    monkeypatch.delenv("CRASHGUARD_DATADOG_QUERY_FATAL", raising=False)
    monkeypatch.delenv("CRASHGUARD_DATADOG_QUERY_NONFATAL", raising=False)

    from unittest.mock import patch
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    with patch("app.crashguard.config._load_yaml", return_value={
        "crashguard": {
            "datadog": {
                "query_fatal": "@error.is_crash:true",
                "query_non_fatal": "@type:error -@error.is_crash:true",
            }
        }
    }):
        s = get_crashguard_settings()

    assert s.datadog_query_fatal == "@error.is_crash:true"
    assert s.datadog_query_nonfatal == "@type:error -@error.is_crash:true"
