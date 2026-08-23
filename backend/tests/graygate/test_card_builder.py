"""card_builder.py 单测——mock httpx，不打真实 Datadog API。"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

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


def test_tier_md_includes_active_users_line_when_provided():
    spec = MetricSpec(key="fps", title="FPS", cell_format="{v:.2f}")
    cells = {"fps": cb._Cell(value=60.0, sentinel=None)}
    lines = cb._tier_md("__大盘（4.0.3*）__", [spec], cells, active_users=1005)
    assert "· 活跃用户数（session 代理）：1,005" in lines
    # 排在最前面（标题之后，指标之前），不是随便插在末尾
    assert lines.index("· 活跃用户数（session 代理）：1,005") < lines.index("· FPS：60.00")


def test_tier_md_omits_active_users_line_when_not_provided():
    """默认行为不变——不传 active_users 就完全不出现这一行（旧调用点/旧测试不受影响）。"""
    spec = MetricSpec(key="fps", title="FPS", cell_format="{v:.2f}")
    cells = {"fps": cb._Cell(value=60.0, sentinel=None)}
    lines = cb._tier_md("__大盘（4.0.3*）__", [spec], cells)
    assert not any("活跃用户数" in ln for ln in lines)


def test_tier_md_active_users_line_present_even_when_cells_none():
    """gate 跳过整层（cells=None，比如样本不足）时，如果有 active_users 数字仍应展示——
    "活跃用户数是多少"跟"其它指标要不要查"是两件独立的事。"""
    lines = cb._tier_md("__大盘（4.0.3*）__", [], None, note=cb._NO_DATA, active_users=42)
    assert "· 活跃用户数（session 代理）：42" in lines


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
    # 可点击范围扩大到整个标题，不是只有一个箭头符号
    assert "[java.net.SocketException →](https://x/issue/abc)" in md


def test_build_top_crash_md_wraps_whole_title_in_link():
    from app.graygate.services.top_issues import TopCrash
    crash = TopCrash(platform="ios", events_count=100, title="SIGABRT",
                      datadog_url="https://x/issue/def")
    md = cb._build_top_crash_md([crash])
    assert md is not None
    assert "[SIGABRT →](https://x/issue/def)" in md


def test_build_top_jank_md_includes_clickable_link():
    from app.graygate.services.top_issues import TopJank
    jank = TopJank(platform="android", label="Foo.bar", events_count=8,
                   datadog_url="https://x/logs?query=abc")
    md = cb._build_top_jank_md([jank])
    assert md is not None
    assert "[Foo.bar →](https://x/logs?query=abc)" in md


@pytest.mark.asyncio
async def test_resolve_cell_widget_lookup_failure_degrades_not_raises(monkeypatch):
    """这是真正在生产环境跑的 _resolve_cell（scheduler.py 走的是
    card_builder.build_report_card，不是 report_builder.build_report）。
    2026-08-23 事故：这里之前没 catch get_metric_scalar 的 ValueError，一个
    widget 标题问题直接崩穿整份报告。"""
    async def _raising_get_metric_scalar(dashboard_json, title, template_vars, from_ms, to_ms):
        raise ValueError(f"widget title not found on dashboard: {title!r}")

    monkeypatch.setattr(cb, "get_metric_scalar", _raising_get_metric_scalar)
    spec = MetricSpec(key="hang_rate", title="Hang Rate (iOS only)", cell_format="{v:.3f}ms/hr")

    cell = await cb._resolve_cell({"widgets": []}, spec, "ios", "4.0.301-1043", None, {}, 0, 1000)

    assert cell.value is None
    assert cell.sentinel == cb._QUERY_FAILED


@pytest.mark.asyncio
async def test_build_report_card_one_metric_failure_does_not_crash_whole_card(monkeypatch):
    """端到端：一个指标的 widget 标题查不到，整份卡片仍要正常生成，
    其它指标照常展示，不是抛异常把整个 build_report_card 炸穿。"""
    async def fake_resolve_versions(from_ms, to_ms):
        pv = PlatformVersions(
            platform="ios", versions=[("4.0.301-1043", 1000)],
            top_version="4.0.301-1043", top_version_events=1000, total_events=1005,
        )
        empty = PlatformVersions(platform="android")
        return {"ios": pv, "android": empty}

    async def fake_get_dashboard_json(dashboard_id):
        return {"widgets": []}

    class _FakeMetricsConfig:
        template_variables = {"env": "production"}
        metrics = [
            MetricSpec(key="fps", title="FPS", cell_format="{v:.2f}"),
            MetricSpec(key="hang_rate", title="Hang Rate (iOS only)", cell_format="{v:.3f}ms/hr"),
        ]

    def fake_load_metrics_config():
        return _FakeMetricsConfig()

    async def fake_get_metric_scalar(dashboard_json, title, template_vars, from_ms, to_ms):
        if title == "Hang Rate (iOS only)":
            raise ValueError(f"widget title not found on dashboard: {title!r}")
        return 60.0

    async def _fake_empty_list(*args, **kwargs):
        return []

    monkeypatch.setattr(cb, "resolve_versions", fake_resolve_versions)
    monkeypatch.setattr(cb, "get_dashboard_json", fake_get_dashboard_json)
    monkeypatch.setattr(cb, "load_metrics_config", fake_load_metrics_config)
    monkeypatch.setattr(cb, "get_metric_scalar", fake_get_metric_scalar)
    monkeypatch.setattr(cb, "find_new_crashes", _fake_empty_list)
    monkeypatch.setattr(cb, "find_top_crashes", _fake_empty_list)
    monkeypatch.setattr(cb, "find_top_jank", _fake_empty_list)
    monkeypatch.setattr(cb, "get_focus_version", AsyncMock(return_value=None))

    result = await cb.build_report_card(date(2026, 8, 18))  # 不应该抛异常

    assert result.available is True
    ios_text = _ios_column_text(result)
    assert "60.00" in ios_text  # fps 照常出数据
    assert cb._QUERY_FAILED in ios_text  # hang_rate 降级成取数失败占位


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
async def _run_build_report_card_with(monkeypatch, focus_version_return):
    """公共 harness：mock 掉 resolve_versions/dashboard/metrics/新增崩溃/Top5，
    只留 focus_version 这一个变量，供覆盖逻辑相关测试复用。"""
    async def fake_resolve_versions(from_ms, to_ms):
        pv = PlatformVersions(
            platform="ios", versions=[("4.0.301-1043", 1000), ("4.0.302-2000", 5)],
            top_version="4.0.301-1043", top_version_events=1000,
            total_events=1005,
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
        return 60.0

    async def _fake_empty_list(*args, **kwargs):
        return []

    monkeypatch.setattr(cb, "resolve_versions", fake_resolve_versions)
    monkeypatch.setattr(cb, "get_dashboard_json", fake_get_dashboard_json)
    monkeypatch.setattr(cb, "load_metrics_config", fake_load_metrics_config)
    monkeypatch.setattr(cb, "get_metric_scalar", fake_get_metric_scalar)
    monkeypatch.setattr(cb, "find_new_crashes", _fake_empty_list)
    monkeypatch.setattr(cb, "find_top_crashes", _fake_empty_list)
    monkeypatch.setattr(cb, "find_top_jank", _fake_empty_list)
    monkeypatch.setattr(cb, "get_focus_version", AsyncMock(return_value=focus_version_return))

    return await cb.build_report_card(date(2026, 8, 18))


def _ios_column_text(result):
    column_set = next(e for e in result.card["body"]["elements"] if e.get("tag") == "column_set")
    return column_set["columns"][0]["elements"][0]["text"]["content"]


@pytest.mark.asyncio
async def test_build_report_card_uses_focus_version_override_when_set(monkeypatch):
    """人工指定的 focus version 存在时，"主要版本"层应该跟踪它，而不是
    session 数自动判定的 top_version；session 数从 pv.versions 里查（这里是
    4.0.302-2000 = 5，不是 top_version 的 1000），并标注"（人工指定）"。"""
    result = await _run_build_report_card_with(monkeypatch, "4.0.302-2000")
    assert result.available is True
    ios_col = _ios_column_text(result)
    assert "__主要版本__ `4.0.302-2000`（5 sessions）（人工指定）" in ios_col
    assert "4.0.301-1043" not in ios_col  # 不应该还在用自动判定的 top_version


@pytest.mark.asyncio
async def test_build_report_card_falls_back_to_top_version_without_override(monkeypatch):
    """未设置 focus version（返回 None）时，"主要版本"层回落到 session 数
    自动判定的 top_version，不带"人工指定"标注。"""
    result = await _run_build_report_card_with(monkeypatch, None)
    assert result.available is True
    ios_col = _ios_column_text(result)
    assert "__主要版本__ `4.0.301-1043`（1,000 sessions）" in ios_col
    assert "人工指定" not in ios_col


@pytest.mark.asyncio
async def test_build_report_card_shows_active_users_for_market_tier(monkeypatch):
    """大盘层要展示"活跃用户数"（session 代理，pv.total_events）——这是这次新加的
    需求；主要版本层的标题本来就带"(N sessions)"括注，不重复展示同一个数字。"""
    result = await _run_build_report_card_with(monkeypatch, None)
    ios_col = _ios_column_text(result)
    assert "· 活跃用户数（session 代理）：1,005" in ios_col
