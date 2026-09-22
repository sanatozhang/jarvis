"""符号缓存保留版本数默认值护栏（2026-09-22）。

10 → 20 的背景：symbol_prewarmer 会主动占满 github_cache 名额（按 graygate
主要版本预下载），keep=10 时会把研发正在回查的老版本挤掉。两个配置必须一起提，
否则会出现「为什么老版本有时在有时不在」的认知不一致。
"""
from __future__ import annotations

from app.crashguard.config import CrashguardSettings


def test_symbol_keep_versions_defaults_are_20():
    s = CrashguardSettings()
    assert s.symbol_upload_keep_versions == 20
    assert s.github_cache_keep_versions == 20


def test_keep_versions_within_settings_page_range():
    """/settings 页面的输入范围是 1–50，默认值必须落在里面。"""
    s = CrashguardSettings()
    for v in (s.symbol_upload_keep_versions, s.github_cache_keep_versions):
        assert 1 <= v <= 50
