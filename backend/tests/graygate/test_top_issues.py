"""top_issues.py 单测——mock DatadogClient 方法，不打真实 API。"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.graygate.services import top_issues as ti


def _raw_issue(platform, events, title, issue_id="abc", version="4.0.301-1038"):
    return {
        "id": issue_id,
        "type": "error_tracking_issue",
        "attributes": {
            "title": title,
            "service": f"plaud_{platform.lower()}",
            "platform": platform,
            "first_seen_timestamp": None,
            "last_seen_timestamp": None,
            "first_seen_version": version,
            "last_seen_version": version,
            "events_count": events,
            "users_affected": 0,
            "sessions_affected": 0,
            "stack_trace": "",
            "tags": {},
        },
    }


@pytest.mark.asyncio
async def test_find_top_crashes_sorted_desc_and_capped_at_5():
    raw = [_raw_issue("IOS", n, f"issue-{n}") for n in [5, 999, 42, 100, 7, 300]]
    with patch(
        "app.graygate.services.top_issues.DatadogClient.list_issues_for_window",
        new=AsyncMock(return_value=raw),
    ):
        out = await ti.find_top_crashes(date(2026, 8, 18))
    assert len(out) == 5
    assert [c.events_count for c in out] == [999, 300, 100, 42, 7]
    assert out[0].platform == "ios"


@pytest.mark.asyncio
async def test_find_top_crashes_circuit_breaker_returns_empty():
    from app.crashguard.services.datadog_client import CircuitBreakerOpen
    with patch(
        "app.graygate.services.top_issues.DatadogClient.list_issues_for_window",
        new=AsyncMock(side_effect=CircuitBreakerOpen("busy")),
    ):
        out = await ti.find_top_crashes(date(2026, 8, 18))
    assert out == []


def test_jank_group_prefers_app_stack_frame():
    attrs = {"has_app_frame": True, "app_stack_frame": "ai.plaud.android.payment.k.a"}
    label, frag = ti._jank_group(attrs)
    assert label == "ai.plaud.android.payment.k.a"
    assert frag == '@app_stack_frame:"ai.plaud.android.payment.k.a"'


def test_jank_group_falls_back_to_module_offset():
    attrs = {"has_app_frame": True, "app_stack_module": "PlaudCore", "app_stack_module_offset": "0x1a2b"}
    label, frag = ti._jank_group(attrs)
    assert label == "PlaudCore+0x1a2b"
    assert frag == '@app_stack_module:"PlaudCore" @app_stack_module_offset:"0x1a2b"'


def test_jank_group_falls_back_to_stack_top():
    attrs = {"has_app_frame": False, "stack_top_module": "libc.so", "stack_top_symbol": "memcpy"}
    label, frag = ti._jank_group(attrs)
    assert label == "libc.so.memcpy"
    assert frag == '@stack_top_module:"libc.so" @stack_top_symbol:"memcpy"'


def test_jank_group_unknown_when_nothing_present():
    label, frag = ti._jank_group({})
    assert label == "unknown"
    assert frag == ""


def _log_event(platform, frame):
    return {"attributes": {"attributes": {
        "os": {"name": platform}, "has_app_frame": True, "app_stack_frame": frame,
    }}}


@pytest.mark.asyncio
async def test_find_top_jank_aggregates_by_platform_and_label():
    page1 = {
        "data": [
            _log_event("ios", "A.foo"),
            _log_event("ios", "A.foo"),
            _log_event("android", "B.bar"),
        ],
        "next_cursor": None,
    }
    with patch(
        "app.graygate.services.top_issues.DatadogClient.search_logs_page",
        new=AsyncMock(return_value=page1),
    ):
        out = await ti.find_top_jank(date(2026, 8, 18))
    assert len(out) == 2
    assert out[0].platform == "ios"
    assert out[0].label == "A.foo"
    assert out[0].events_count == 2
    assert out[1].events_count == 1
    # 深链：包含平台过滤 + 该分组的查询片段，能定位到具体这一组卡顿日志
    assert "app_stack_frame" in out[0].datadog_url
    assert "os.name%3Aios" in out[0].datadog_url or "os.name:ios" in out[0].datadog_url
    assert "/logs?query=" in out[0].datadog_url


@pytest.mark.asyncio
async def test_find_top_jank_deep_link_preserves_original_os_name_case():
    """真实 Datadog Logs 里 @os.name 是大小写敏感的原始值（"iOS"/"Android"，
    不是全小写）——2026-08-19 实测发现深链拼小写 @os.name:ios 会导致 0 命中，
    必须用原始大小写 @os.name:iOS 才能真正匹配。这里 mock 一个大小写混合的
    os.name（"iOS"）断言深链里保留了原始大小写，不是内部分组用的小写 platform。
    """
    page = {"data": [_log_event("iOS", "A.foo")], "next_cursor": None}
    with patch(
        "app.graygate.services.top_issues.DatadogClient.search_logs_page",
        new=AsyncMock(return_value=page),
    ):
        out = await ti.find_top_jank(date(2026, 8, 18))
    assert out[0].platform == "ios"  # 展示/分组仍用小写，跟 TopCrash/NewCrash 一致
    assert "os.name%3AiOS" in out[0].datadog_url  # 但深链查询用原始大小写 "iOS"
    assert "os.name%3Aios" not in out[0].datadog_url


@pytest.mark.asyncio
async def test_find_top_jank_paginates_until_no_cursor():
    page1 = {"data": [_log_event("ios", "A.foo")], "next_cursor": "cursor1"}
    page2 = {"data": [_log_event("ios", "A.foo")], "next_cursor": None}
    with patch(
        "app.graygate.services.top_issues.DatadogClient.search_logs_page",
        new=AsyncMock(side_effect=[page1, page2]),
    ):
        out = await ti.find_top_jank(date(2026, 8, 18))
    assert len(out) == 1
    assert out[0].events_count == 2


@pytest.mark.asyncio
async def test_find_top_jank_stops_at_max_pages():
    page = {"data": [_log_event("ios", "A.foo")], "next_cursor": "keep-going"}
    with patch(
        "app.graygate.services.top_issues.DatadogClient.search_logs_page",
        new=AsyncMock(return_value=page),
    ):
        out = await ti.find_top_jank(date(2026, 8, 18))
    # 每页 1 个匹配事件，20 页上限 -> 精确 20 个事件计入同一 key
    assert out[0].events_count == ti._JANK_MAX_PAGES


@pytest.mark.asyncio
async def test_find_top_jank_circuit_breaker_returns_empty():
    from app.crashguard.services.datadog_client import CircuitBreakerOpen
    with patch(
        "app.graygate.services.top_issues.DatadogClient.search_logs_page",
        new=AsyncMock(side_effect=CircuitBreakerOpen("busy")),
    ):
        out = await ti.find_top_jank(date(2026, 8, 18))
    assert out == []


@pytest.mark.asyncio
async def test_find_top_jank_skips_events_without_platform():
    page = {
        "data": [
            {"attributes": {"attributes": {"os": {}, "app_stack_frame": "X"}}},
            _log_event("android", "Y.z"),
        ],
        "next_cursor": None,
    }
    with patch(
        "app.graygate.services.top_issues.DatadogClient.search_logs_page",
        new=AsyncMock(return_value=page),
    ):
        out = await ti.find_top_jank(date(2026, 8, 18))
    assert len(out) == 1
    assert out[0].platform == "android"
