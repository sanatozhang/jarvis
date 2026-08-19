"""graygate.services.report_builder 单测。

全部 mock `resolve_versions` / `get_dashboard_json` / `get_metric_scalar` /
`load_metrics_config` / `find_new_crashes`，不打真实 Datadog API（这几个函数
本身在各自模块的单测里已经锁定行为，这里只测试 report_builder 的编排/渲染逻辑）。

核心锁定点（详见 task-5-brief.md 「验证要求」）：

1. 两个平台 `top_version` 都是 None → `available=False, markdown=""`，且不再
   往下发起任何 Datadog 查询（load_metrics_config / get_dashboard_json /
   get_metric_scalar 均不应被调用）。
2. 无恶化、无新增崩溃 → markdown 不包含"恶化"/"新增崩溃"关键词。
3. 恶化案例：crash_free 昨日 99.0% vs 前日 99.8%，跌 0.8pp（>0.5pp 阈值），
   directionality=increase_better → 恶化段出现，方向箭头 ▼ 正确。
4. 同样跌破阈值但方向是变好（crash_free 涨 0.8pp）→ 不出现在恶化段。
5. not_applicable_platform 指标（如 android_anr 对 ios）→ 该单元格
   "—（不适用）"，且没有为 (ios, android_anr) 这个组合发起 get_metric_scalar 调用。
6. min_sessions 地板：top_version_events 低于 settings.min_sessions → 该
   "最新版"列单元格 "—（样本不足）"，同样不发起查询。
7. 新增崩溃非空 → "新增崩溃堆栈" 段落出现，且内容包含 datadog_url。

额外覆盖（编排规则里明确写了，但不在「验证要求」编号列表里，一并锁定）：

8. 只有一个平台有 top_version → 另一个平台的"最新版"列显示"—（无数据）"，
   但"大盘"列仍然正常查询（不依赖 resolve_versions 是否枚举到具体 build）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pytest

from app.graygate.services import report_builder as rb
from app.graygate.services.dashboard_query import MetricSpec, MetricsConfig
from app.graygate.services.new_crashes import NewCrash

TARGET_DATE = date(2026, 8, 18)
PREV_DATE = TARGET_DATE - timedelta(days=1)

Y_FROM, Y_TO = rb._window_ms(TARGET_DATE)
P_FROM, P_TO = rb._window_ms(PREV_DATE)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakePlatformVersions:
    platform: str
    top_version: Optional[str] = None
    top_version_events: int = 0
    total_events: int = 0
    versions: list = field(default_factory=list)


def _settings(min_sessions: int = 50, version_pattern: str = "4.0.3*") -> SimpleNamespace:
    return SimpleNamespace(
        dashboard_id="dash1",
        version_pattern=version_pattern,
        min_sessions=min_sessions,
    )


def _metrics_config(metrics: List[MetricSpec]) -> MetricsConfig:
    return MetricsConfig(
        dashboard_id="dash1",
        template_variables={"env": "production", "os_version": "*", "usr.id": "*"},
        metrics=metrics,
    )


def _widget(directionality: str = "increase_better") -> dict:
    return {"definition": {"requests": [{"comparison": {"directionality": directionality}}]}}


def _dashboard_json(titles_directionality: Dict[str, str]) -> dict:
    widgets = []
    for title, directionality in titles_directionality.items():
        w = _widget(directionality)
        w["definition"]["title"] = title
        widgets.append(w)
    return {"widgets": widgets}


class _ScalarStub:
    """Records every get_metric_scalar call and returns a value looked up by
    (widget_title, service, version, day)."""

    def __init__(self, values: Dict[Tuple[str, str, str, str], float]):
        self.values = values
        self.calls: List[Tuple[str, str, Optional[str], str]] = []

    async def __call__(self, dashboard_json, widget_title, template_vars, from_ms, to_ms):
        day = "yesterday" if from_ms == Y_FROM else "prev"
        service = template_vars.get("service")
        version = template_vars.get("version")
        self.calls.append((widget_title, service, version, day))
        return self.values.get((widget_title, service, version, day))

    def called_with(self, widget_title: str, service: str) -> bool:
        return any(c[0] == widget_title and c[1] == service for c in self.calls)


def _patch_common(
    monkeypatch,
    versions: Dict[str, _FakePlatformVersions],
    metrics_config: MetricsConfig,
    dashboard_json: dict,
    scalar_stub: _ScalarStub,
    new_crashes: Optional[List[NewCrash]] = None,
    settings: Optional[SimpleNamespace] = None,
):
    monkeypatch.setattr(rb, "get_graygate_settings", lambda: settings or _settings())

    async def fake_resolve_versions(from_ms, to_ms):
        return versions

    monkeypatch.setattr(rb, "resolve_versions", fake_resolve_versions)
    monkeypatch.setattr(rb, "load_metrics_config", lambda: metrics_config)

    async def fake_get_dashboard_json(dashboard_id):
        return dashboard_json

    monkeypatch.setattr(rb, "get_dashboard_json", fake_get_dashboard_json)
    monkeypatch.setattr(rb, "get_metric_scalar", scalar_stub)

    async def fake_find_new_crashes(target_date):
        return new_crashes or []

    monkeypatch.setattr(rb, "find_new_crashes", fake_find_new_crashes)


# ---------------------------------------------------------------------------
# 1. Both platforms empty -> unavailable, no further queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_when_both_platforms_have_no_top_version(monkeypatch):
    versions = {
        "ios": _FakePlatformVersions(platform="ios"),
        "android": _FakePlatformVersions(platform="android"),
    }

    load_metrics_called = False

    def fake_load_metrics_config():
        nonlocal load_metrics_called
        load_metrics_called = True
        return _metrics_config([])

    monkeypatch.setattr(rb, "get_graygate_settings", lambda: _settings())

    async def fake_resolve_versions(from_ms, to_ms):
        return versions

    monkeypatch.setattr(rb, "resolve_versions", fake_resolve_versions)
    monkeypatch.setattr(rb, "load_metrics_config", fake_load_metrics_config)

    result = await rb.build_report(TARGET_DATE)

    assert result.available is False
    assert result.markdown == ""
    assert load_metrics_called is False


# ---------------------------------------------------------------------------
# 2. No anomalies -> no "恶化"/"新增崩溃" keywords
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_anomalies_omits_worsen_and_new_crash_sections(monkeypatch):
    versions = {
        "ios": _FakePlatformVersions(platform="ios", top_version="4.0.301-1013", top_version_events=500, total_events=1000),
        "android": _FakePlatformVersions(platform="android", top_version="4.0.302-2020", top_version_events=600, total_events=1200),
    }
    crash_free = MetricSpec(key="crash_free", title="Crash-free sessions", cell_format="{v:.2f}%")
    metrics_config = _metrics_config([crash_free])
    dashboard_json = _dashboard_json({"Crash-free sessions": "increase_better"})

    # No change day over day, for both platforms -> nothing breaches.
    values = {}
    for platform, service, version in (
        ("ios", "plaud_ios", "4.0.301-1013"),
        ("android", "plaud_android", "4.0.302-2020"),
    ):
        for day in ("yesterday", "prev"):
            values[("Crash-free sessions", service, version, day)] = 99.5
        for day in ("yesterday", "prev"):
            values[("Crash-free sessions", service, "4.0.3*", day)] = 99.5

    scalar_stub = _ScalarStub(values)
    _patch_common(monkeypatch, versions, metrics_config, dashboard_json, scalar_stub)

    result = await rb.build_report(TARGET_DATE)

    assert result.available is True
    assert "恶化" not in result.markdown
    assert "新增崩溃" not in result.markdown
    assert "全量指标" in result.markdown  # 折叠区始终存在


# ---------------------------------------------------------------------------
# 3 & 4. Direction-aware worsen detection
# ---------------------------------------------------------------------------


def _crash_free_scenario(yesterday_val: float, prev_val: float):
    versions = {
        "ios": _FakePlatformVersions(platform="ios", top_version="4.0.301-1013", top_version_events=500, total_events=1000),
        "android": _FakePlatformVersions(platform="android", top_version="4.0.302-2020", top_version_events=600, total_events=1200),
    }
    crash_free = MetricSpec(key="crash_free", title="Crash-free sessions", cell_format="{v:.2f}%")
    metrics_config = _metrics_config([crash_free])
    dashboard_json = _dashboard_json({"Crash-free sessions": "increase_better"})

    values = {
        ("Crash-free sessions", "plaud_ios", "4.0.301-1013", "yesterday"): yesterday_val,
        ("Crash-free sessions", "plaud_ios", "4.0.301-1013", "prev"): prev_val,
        # Android + 大盘列保持不变，避免噪音
        ("Crash-free sessions", "plaud_android", "4.0.302-2020", "yesterday"): 99.5,
        ("Crash-free sessions", "plaud_android", "4.0.302-2020", "prev"): 99.5,
        ("Crash-free sessions", "plaud_ios", "4.0.3*", "yesterday"): 99.5,
        ("Crash-free sessions", "plaud_ios", "4.0.3*", "prev"): 99.5,
        ("Crash-free sessions", "plaud_android", "4.0.3*", "yesterday"): 99.5,
        ("Crash-free sessions", "plaud_android", "4.0.3*", "prev"): 99.5,
    }
    return versions, metrics_config, dashboard_json, values


@pytest.mark.asyncio
async def test_worsen_detected_with_correct_direction_and_arrow(monkeypatch):
    # crash_free dropped 0.8pp (99.0 vs 99.8), directionality=increase_better -> a drop is worse.
    versions, metrics_config, dashboard_json, values = _crash_free_scenario(99.0, 99.8)
    scalar_stub = _ScalarStub(values)
    _patch_common(monkeypatch, versions, metrics_config, dashboard_json, scalar_stub)

    result = await rb.build_report(TARGET_DATE)

    assert "🔴 恶化（DoD 超阈值）" in result.markdown
    assert "iOS" in result.markdown
    assert "▼" in result.markdown
    assert "-0.80pp" in result.markdown


@pytest.mark.asyncio
async def test_direction_aware_improvement_not_flagged_as_worsen(monkeypatch):
    # crash_free rose 0.8pp (99.8 vs 99.0) -- an increase is GOOD under increase_better,
    # even though the absolute change clears the 0.5pp threshold.
    versions, metrics_config, dashboard_json, values = _crash_free_scenario(99.8, 99.0)
    scalar_stub = _ScalarStub(values)
    _patch_common(monkeypatch, versions, metrics_config, dashboard_json, scalar_stub)

    result = await rb.build_report(TARGET_DATE)

    assert "恶化" not in result.markdown


# ---------------------------------------------------------------------------
# 5. not_applicable_platform -> placeholder cell, no query for that combo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_applicable_platform_renders_placeholder_and_skips_query(monkeypatch):
    versions = {
        "ios": _FakePlatformVersions(platform="ios", top_version="4.0.301-1013", top_version_events=500, total_events=1000),
        "android": _FakePlatformVersions(platform="android", top_version="4.0.302-2020", top_version_events=600, total_events=1200),
    }
    android_anr = MetricSpec(
        key="android_anr", title="Android ANR", cell_format="{v:.2f}%",
        not_applicable_platform="ios",
    )
    metrics_config = _metrics_config([android_anr])
    dashboard_json = _dashboard_json({"Android ANR": "decrease_better"})

    values = {
        ("Android ANR", "plaud_android", "4.0.302-2020", "yesterday"): 0.10,
        ("Android ANR", "plaud_android", "4.0.302-2020", "prev"): 0.10,
        ("Android ANR", "plaud_android", "4.0.3*", "yesterday"): 0.12,
        ("Android ANR", "plaud_android", "4.0.3*", "prev"): 0.12,
    }
    scalar_stub = _ScalarStub(values)
    _patch_common(monkeypatch, versions, metrics_config, dashboard_json, scalar_stub)

    result = await rb.build_report(TARGET_DATE)

    assert "—（不适用）" in result.markdown
    assert scalar_stub.called_with("Android ANR", "plaud_ios") is False
    # Android side should have queried normally.
    assert scalar_stub.called_with("Android ANR", "plaud_android") is True


# ---------------------------------------------------------------------------
# 6. min_sessions floor -> insufficient-sample placeholder, no query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_sessions_floor_renders_insufficient_sample_and_skips_query(monkeypatch):
    versions = {
        # top_version_events (10) below min_sessions (50) -> "最新版" col gated.
        "ios": _FakePlatformVersions(platform="ios", top_version="4.0.301-1013", top_version_events=10, total_events=1000),
        "android": _FakePlatformVersions(platform="android", top_version="4.0.302-2020", top_version_events=600, total_events=1200),
    }
    fps = MetricSpec(key="fps", title="APP单次运行平均FPS", cell_format="{v:.2f}")
    metrics_config = _metrics_config([fps])
    dashboard_json = _dashboard_json({"APP单次运行平均FPS": "increase_better"})

    values = {
        ("APP单次运行平均FPS", "plaud_ios", "4.0.3*", "yesterday"): 58.0,
        ("APP单次运行平均FPS", "plaud_ios", "4.0.3*", "prev"): 58.0,
        ("APP单次运行平均FPS", "plaud_android", "4.0.302-2020", "yesterday"): 59.0,
        ("APP单次运行平均FPS", "plaud_android", "4.0.302-2020", "prev"): 59.0,
        ("APP单次运行平均FPS", "plaud_android", "4.0.3*", "yesterday"): 59.0,
        ("APP单次运行平均FPS", "plaud_android", "4.0.3*", "prev"): 59.0,
    }
    scalar_stub = _ScalarStub(values)
    _patch_common(monkeypatch, versions, metrics_config, dashboard_json, scalar_stub, settings=_settings(min_sessions=50))

    result = await rb.build_report(TARGET_DATE)

    assert "—（样本不足）" in result.markdown
    # The gated (ios, top) combo must never have been queried.
    assert ("APP单次运行平均FPS", "plaud_ios", "4.0.301-1013", "yesterday") not in scalar_stub.calls
    assert not any(
        c[0] == "APP单次运行平均FPS" and c[1] == "plaud_ios" and c[2] == "4.0.301-1013"
        for c in scalar_stub.calls
    )
    # ios market column is NOT gated by the top-scope floor -> should still be queried.
    assert scalar_stub.called_with("APP单次运行平均FPS", "plaud_ios") is True


# ---------------------------------------------------------------------------
# 7. New crashes section
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_crashes_section_includes_datadog_url(monkeypatch):
    versions = {
        "ios": _FakePlatformVersions(platform="ios", top_version="4.0.301-1013", top_version_events=500, total_events=1000),
        "android": _FakePlatformVersions(platform="android", top_version="4.0.302-2020", top_version_events=600, total_events=1200),
    }
    crash_free = MetricSpec(key="crash_free", title="Crash-free sessions", cell_format="{v:.2f}%")
    metrics_config = _metrics_config([crash_free])
    dashboard_json = _dashboard_json({"Crash-free sessions": "increase_better"})

    values = {}
    for service, version in (("plaud_ios", "4.0.301-1013"), ("plaud_android", "4.0.302-2020")):
        for v in ("4.0.3*", version):
            for day in ("yesterday", "prev"):
                values[("Crash-free sessions", service, v, day)] = 99.5

    scalar_stub = _ScalarStub(values)
    new_crash = NewCrash(
        platform="ios",
        version="4.0.301-1013",
        events_count=42,
        title="EXC_BAD_ACCESS in Foo.swift",
        datadog_url="https://app.datadoghq.com/error-tracking/issue/abc123",
    )
    _patch_common(monkeypatch, versions, metrics_config, dashboard_json, scalar_stub, new_crashes=[new_crash])

    result = await rb.build_report(TARGET_DATE)

    assert "🆕 新增崩溃堆栈" in result.markdown
    assert "https://app.datadoghq.com/error-tracking/issue/abc123" in result.markdown
    assert "EXC_BAD_ACCESS in Foo.swift" in result.markdown


# ---------------------------------------------------------------------------
# 8. Partial availability: one platform has no top_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_with_no_top_version_shows_no_data_but_market_col_still_queried(monkeypatch):
    # top_version=None but total_events kept above min_sessions on purpose: in
    # the real version_resolver an empty `versions` list means both top_version
    # is None AND total_events is 0 (they're derived from the same buckets), so
    # this exact combination cannot occur end-to-end. But report_builder's own
    # gating logic treats "no top build enumerated" and "market-column sample
    # floor" as two independent checks (per brief rule: 大盘列不依赖
    # resolve_versions 是否枚举到具体 build) -- this test isolates that specific
    # independence rather than reproducing a real version_resolver output.
    versions = {
        "ios": _FakePlatformVersions(platform="ios", top_version=None, top_version_events=0, total_events=1200),
        "android": _FakePlatformVersions(platform="android", top_version="4.0.302-2020", top_version_events=600, total_events=1200),
    }
    crash_free = MetricSpec(key="crash_free", title="Crash-free sessions", cell_format="{v:.2f}%")
    metrics_config = _metrics_config([crash_free])
    dashboard_json = _dashboard_json({"Crash-free sessions": "increase_better"})

    values = {
        ("Crash-free sessions", "plaud_ios", "4.0.3*", "yesterday"): 99.1,
        ("Crash-free sessions", "plaud_ios", "4.0.3*", "prev"): 99.1,
        ("Crash-free sessions", "plaud_android", "4.0.302-2020", "yesterday"): 99.5,
        ("Crash-free sessions", "plaud_android", "4.0.302-2020", "prev"): 99.5,
        ("Crash-free sessions", "plaud_android", "4.0.3*", "yesterday"): 99.5,
        ("Crash-free sessions", "plaud_android", "4.0.3*", "prev"): 99.5,
    }
    scalar_stub = _ScalarStub(values)
    _patch_common(monkeypatch, versions, metrics_config, dashboard_json, scalar_stub)

    result = await rb.build_report(TARGET_DATE)

    assert result.available is True
    assert "iOS 主力 —（无数据）" in result.markdown
    assert "iOS 最新版 —（无数据）" in result.markdown
    # ios market column used the wildcard version and got queried normally.
    assert scalar_stub.called_with("Crash-free sessions", "plaud_ios") is True
    assert "iOS 大盘 99.10%" in result.markdown
