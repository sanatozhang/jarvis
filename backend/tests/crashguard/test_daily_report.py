"""daily_report 服务单测"""
from __future__ import annotations

import pytest


def test_parse_top_app_version_basic():
    from app.crashguard.services.daily_report import _parse_top_app_version
    out = _parse_top_app_version("3.15.1-630 (87.4%), 3.16.0-634 (8.2%), 3.14.0-620 (2.8%)")
    assert out == [
        ("3.15.1-630", 87.4),
        ("3.16.0-634", 8.2),
        ("3.14.0-620", 2.8),
    ]


def test_parse_top_app_version_empty():
    from app.crashguard.services.daily_report import _parse_top_app_version
    assert _parse_top_app_version("") == []
    assert _parse_top_app_version(None) == []


def test_parse_top_app_version_malformed():
    """残缺数据不应崩溃"""
    from app.crashguard.services.daily_report import _parse_top_app_version
    out = _parse_top_app_version("noPctHere, 3.0 (50%)")
    assert out == [("3.0", 50.0)]


def test_delta_pct_basic():
    from app.crashguard.services.daily_report import _delta_pct
    assert _delta_pct(120, 100) == pytest.approx(0.2)
    assert _delta_pct(80, 100) == pytest.approx(-0.2)


def test_delta_pct_no_yesterday():
    """昨日 0 / None → 返回 None（避免除零）"""
    from app.crashguard.services.daily_report import _delta_pct
    assert _delta_pct(100, None) is None
    assert _delta_pct(100, 0) is None


def test_resolve_real_os_native():
    from app.crashguard.services.daily_report import _resolve_real_os
    assert _resolve_real_os("ANDROID", "") == "ANDROID"
    assert _resolve_real_os("ios", "") == "IOS"


def test_resolve_real_os_flutter_with_top_os():
    from app.crashguard.services.daily_report import _resolve_real_os
    # 主体 iOS
    assert _resolve_real_os("flutter", "iOS 17 (80%), Android 14 (20%)") == "IOS"
    # 主体 Android
    assert _resolve_real_os("flutter", "Android 14 (60%), iOS 17 (40%)") == "ANDROID"
    # iPadOS 也归 iOS
    assert _resolve_real_os("flutter", "iPadOS 17 (90%)") == "IOS"


def test_resolve_real_os_flutter_no_top_os_returns_none():
    """Flutter 无 top_os → 直接忽略，不归类"""
    from app.crashguard.services.daily_report import _resolve_real_os
    assert _resolve_real_os("flutter", "") is None


def test_resolve_real_os_browser_ignored():
    from app.crashguard.services.daily_report import _resolve_real_os
    assert _resolve_real_os("browser", "") is None
    assert _resolve_real_os("desktop", "Windows 11 (50%)") is None


def test_line_for_issue_with_delta():
    from app.crashguard.services.daily_report import _line_for_issue
    line = _line_for_issue("abc-123", "Test crash", 1234, 0.25)
    assert "1,234" in line
    assert "+25%" in line
    assert "Test crash" in line


def test_line_for_issue_no_delta_no_new():
    """无昨日对比 + 非新版 → 显示 '—' 而非误标 🆕"""
    from app.crashguard.services.daily_report import _line_for_issue
    line = _line_for_issue("abc", "T", 100, None, is_new_in_version=False)
    assert "—" in line
    assert "🆕" not in line


def test_line_for_issue_new_in_version_no_delta():
    """无昨日 + is_new_in_version=True → 显示 🆕新版"""
    from app.crashguard.services.daily_report import _line_for_issue
    line = _line_for_issue("abc", "T", 100, None, is_new_in_version=True)
    assert "🆕新版" in line
