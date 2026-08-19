"""card_builder.py 单测——mock httpx，不打真实 Datadog API。"""
from __future__ import annotations

from datetime import date

import pytest

from app.graygate.services import card_builder as cb
from app.graygate.services.dashboard_query import MetricSpec
from app.graygate.services.new_crashes import NewCrash
from app.graygate.services.version_resolver import PlatformVersions


def test_status_dot_ge_target():
    assert cb._status_dot({"op": ">=", "value": 60}, 65) == "🟩 "
    assert cb._status_dot({"op": ">=", "value": 60}, 55) == "🟥 "


def test_status_dot_le_target():
    assert cb._status_dot({"op": "<=", "value": 80}, 50) == "🟩 "
    assert cb._status_dot({"op": "<=", "value": 80}, 100) == "🟥 "


def test_status_dot_no_target_or_no_value():
    assert cb._status_dot(None, 100) == ""
    assert cb._status_dot({"op": ">=", "value": 60}, None) == ""


def test_fmt_cell_value_single_widget_with_target():
    spec = MetricSpec(key="fps", title="FPS", cell_format="{v:.2f}", target={"op": ">=", "value": 60})
    cell = cb._Cell(value=65.0, sentinel=None)
    assert cb._fmt_cell_value(spec, cell) == "🟩 65.00"


def test_fmt_cell_value_sentinel_takes_precedence():
    spec = MetricSpec(key="fps", title="FPS", cell_format="{v:.2f}", target={"op": ">=", "value": 60})
    cell = cb._Cell(value=None, sentinel=cb._NOT_APPLICABLE)
    assert cb._fmt_cell_value(spec, cell) == cb._NOT_APPLICABLE


def test_fmt_cell_value_dual_widget_red_if_either_breaches():
    spec = MetricSpec(
        key="jank", title_p75="J75", title_p90="J90", cell_format="{p75:.1f}/{p90:.1f}",
        target_p75={"op": "<=", "value": 20}, target_p90={"op": "<=", "value": 40},
    )
    cell = cb._Cell(value=(5.0, 50.0), sentinel=None)  # p75 ok, p90 breaches
    out = cb._fmt_cell_value(spec, cell)
    assert out.startswith("🟥 ")
    assert "5.0/50.0" in out


def test_build_new_crash_md_empty_returns_none():
    assert cb._build_new_crash_md([]) is None


def test_build_new_crash_md_includes_url_and_platform():
    crash = NewCrash(platform="android", version="4.0.301-1013", events_count=5,
                      title="java.net.SocketException", datadog_url="https://x/issue/abc")
    md = cb._build_new_crash_md([crash])
    assert md is not None
    assert "ANDROID" in md
    assert "https://x/issue/abc" in md
    assert "5" in md


@pytest.mark.asyncio
async def test_build_report_card_unavailable_when_both_platforms_empty(monkeypatch):
    async def fake_resolve_versions(from_ms, to_ms):
        return {
            "ios": PlatformVersions(platform="ios"),
            "android": PlatformVersions(platform="android"),
        }
    monkeypatch.setattr(cb, "resolve_versions", fake_resolve_versions)

    result = await cb.build_report_card(date(2026, 8, 18))
    assert result.available is False
    assert result.card == {}


@pytest.mark.asyncio
async def test_build_report_card_newest_equals_top_skips_redundant_fetch(monkeypatch):
    """newest_version == top_version 时不应该为"最新版本"再发起一次查询——
    通过统计 get_metric_scalar 调用次数验证（只应含 market + top 两轮，不含 newest 轮）。
    """
    fetch_calls = []

    async def fake_resolve_versions(from_ms, to_ms):
        pv = PlatformVersions(
            platform="ios", versions=[("4.0.301-1043", 1000)],
            top_version="4.0.301-1043", top_version_events=1000,
            newest_version="4.0.301-1043", newest_version_events=1000,
            total_events=1000,
        )
        empty = PlatformVersions(platform="android")
        return {"ios": pv, "android": empty}

    async def fake_get_dashboard_json(dashboard_id):
        return {"widgets": []}

    class _FakeMetricsConfig:
        template_variables = {"env": "production"}
        metrics = [MetricSpec(key="fps", title="FPS", cell_format="{v:.2f}")]

    def fake_load_metrics_config():
        return _FakeMetricsConfig()

    async def fake_get_metric_scalar(dashboard_json, title, template_vars, from_ms, to_ms):
        fetch_calls.append(template_vars["version"])
        return 60.0

    async def fake_find_new_crashes(target_date):
        return []

    monkeypatch.setattr(cb, "resolve_versions", fake_resolve_versions)
    monkeypatch.setattr(cb, "get_dashboard_json", fake_get_dashboard_json)
    monkeypatch.setattr(cb, "load_metrics_config", fake_load_metrics_config)
    monkeypatch.setattr(cb, "get_metric_scalar", fake_get_metric_scalar)
    monkeypatch.setattr(cb, "find_new_crashes", fake_find_new_crashes)

    result = await cb.build_report_card(date(2026, 8, 18))
    assert result.available is True
    # ios: market(4.0.3*-pattern via settings) + top(4.0.301-1043) — 不应再出现第三次 4.0.301-1043 查询
    top_build_calls = [v for v in fetch_calls if v == "4.0.301-1043"]
    assert len(top_build_calls) == 1, f"expected exactly 1 call for the shared build, got {len(top_build_calls)}: {fetch_calls}"

    ios_col = result.card["body"]["elements"][2]["columns"][0]["elements"][0]["text"]["content"]
    assert "与主要版本一致" in ios_col
