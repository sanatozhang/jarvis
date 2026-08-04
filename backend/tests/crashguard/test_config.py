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


def test_fatal_backlog_and_pipeline_analyze_max_per_run_defaults(monkeypatch):
    """新增两个配置项（2026-08-04）的默认值：fatal_backlog_max_slots / pipeline_analyze_max_per_run。"""
    monkeypatch.delenv("CRASHGUARD_FATAL_BACKLOG_MAX_SLOTS", raising=False)
    monkeypatch.delenv("CRASHGUARD_PIPELINE_ANALYZE_MAX_PER_RUN", raising=False)

    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    s = get_crashguard_settings()
    assert s.fatal_backlog_max_slots == 3
    assert s.pipeline_analyze_max_per_run == 5


def test_pipeline_analyze_max_per_run_overridden_by_yaml(monkeypatch):
    """`pipeline_analyze_max_per_run` 需能被 config.yaml 顶层 crashguard 段覆盖
    （跟 analyze_top_n 同一个映射列表，写法对齐）。"""
    monkeypatch.delenv("CRASHGUARD_PIPELINE_ANALYZE_MAX_PER_RUN", raising=False)

    from unittest.mock import patch
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    with patch("app.crashguard.config._load_yaml", return_value={
        "crashguard": {"pipeline_analyze_max_per_run": 15}
    }):
        s = get_crashguard_settings()

    assert s.pipeline_analyze_max_per_run == 15


def test_fatal_backlog_max_slots_overridden_by_yaml_thresholds(monkeypatch):
    """`fatal_backlog_max_slots` 需能被 config.yaml crashguard.thresholds 段覆盖
    （跟 jank_attention_min_events 同一个映射列表，写法对齐）。"""
    monkeypatch.delenv("CRASHGUARD_FATAL_BACKLOG_MAX_SLOTS", raising=False)

    from unittest.mock import patch
    from app.crashguard.config import get_crashguard_settings
    get_crashguard_settings.cache_clear()

    with patch("app.crashguard.config._load_yaml", return_value={
        "crashguard": {"thresholds": {"fatal_backlog_max_slots": 7}}
    }):
        s = get_crashguard_settings()

    assert s.fatal_backlog_max_slots == 7


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
