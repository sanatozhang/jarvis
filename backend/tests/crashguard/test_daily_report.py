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


# ── 方案 A：headline 用户中心单一叙事（杜绝"三处口径拼一句"）──────────

def _user_rows(ios=(0, None), android=(0, None)):
    """构造 _compose_headline 需要的 user_plat_rows。(today_crash_users, user_delta_pct)"""
    return [
        {"platform_key": "IOS", "platform_label": "🍎 iOS",
         "today_crash_users": ios[0], "user_delta_pct": ios[1]},
        {"platform_key": "ANDROID", "platform_label": "📱 Android",
         "today_crash_users": android[0], "user_delta_pct": android[1]},
    ]


def test_headline_user_centric_red_no_dimension_mixing():
    """红色 headline 全程讲'用户'：有受影响用户数 + crash-free%，不出现 events%/混维度。"""
    from app.crashguard.services.daily_report import _compose_headline
    lead, breakdown = _compose_headline(
        severity="red",
        user_plat_rows=_user_rows(ios=(321, 3.0), android=(1400, 12.0)),
        new_count=1, surge_count=1, drop_count=0,
        today_fatal_total=3000, base_fatal_total=500,
        total_users=71400, crashed_users=1721,
        base_total_users=70000, base_crashed_users=1500,
    )
    # lead：单一用户主语
    assert "1,721 用户" in lead
    assert "crash-free 97.59%" in lead
    assert "较上周同期" in lead and "pp" in lead
    assert "请工程师立刻跟进" in lead
    # 关键：lead 不得再混 events 百分比（旧 "fatal +509%" 病灶）
    assert "fatal +" not in lead
    assert "events" not in lead
    # 平台拆解全是"人"，按受影响数降序（Android 1400 在 iOS 321 之前）
    assert breakdown[0].startswith("> ├ 📱 Android **1,400 人**（+12% vs 上周）")
    assert breakdown[1].startswith("> └ 🍎 iOS **321 人**（+3% vs 上周）")
    # issue 数明确归"结构"，不混进影响数
    struct = breakdown[-1]
    assert "结构：" in struct and "新增 **1** issue" in struct and "突增 **1** issue" in struct


def test_headline_crash_free_pp_sign():
    """crash-free 较上周恶化 → 负 pp；改善 → 正 pp。"""
    from app.crashguard.services.daily_report import _compose_headline
    # 今日 crash-free 低于上周 → 负 pp
    lead, _ = _compose_headline(
        severity="red", user_plat_rows=_user_rows(android=(200, None)),
        new_count=0, surge_count=0, drop_count=0,
        today_fatal_total=0, base_fatal_total=0,
        total_users=10000, crashed_users=300,      # 97.00%
        base_total_users=10000, base_crashed_users=100,  # 99.00%
    )
    assert "-2.0pp" in lead


def test_headline_platform_no_baseline_marked():
    """平台上周无基线用户数 → 标注'上周无基线'，不编造百分比。"""
    from app.crashguard.services.daily_report import _compose_headline
    _, breakdown = _compose_headline(
        severity="yellow", user_plat_rows=_user_rows(android=(50, None)),
        new_count=0, surge_count=1, drop_count=0,
        today_fatal_total=0, base_fatal_total=0,
        total_users=8000, crashed_users=50,
        base_total_users=0, base_crashed_users=0,
    )
    assert breakdown[0] == "> └ 📱 Android **50 人**（上周无基线）"


def test_headline_fallback_events_only_when_no_user_data():
    """user 数据缺失 → events 单维度兜底，breakdown 为空，不混用户数。"""
    from app.crashguard.services.daily_report import _compose_headline
    lead, breakdown = _compose_headline(
        severity="red", user_plat_rows=_user_rows(),
        new_count=1, surge_count=0, drop_count=0,
        today_fatal_total=3000, base_fatal_total=500,
        total_users=0, crashed_users=0,
        base_total_users=0, base_crashed_users=0,
    )
    assert breakdown == []
    assert "fatal events" in lead
    assert "用户" not in lead  # 没有用户数据就别提用户


def test_headline_green_zero_users_affected():
    """绿色且零用户受影响 → crash-free 100%，安全无虞。"""
    from app.crashguard.services.daily_report import _compose_headline
    lead, breakdown = _compose_headline(
        severity="green", user_plat_rows=_user_rows(),
        new_count=0, surge_count=0, drop_count=0,
        today_fatal_total=0, base_fatal_total=0,
        total_users=10000, crashed_users=0,
        base_total_users=10000, base_crashed_users=0,
    )
    assert "零用户受影响" in lead and "安全无虞" in lead
    assert breakdown == []


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
