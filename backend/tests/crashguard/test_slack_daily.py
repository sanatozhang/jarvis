"""crashguard 早晚报 Slack 精简版（slack_daily.build_daily_slack）。"""
from __future__ import annotations

import json

from app.crashguard.services.slack_daily import build_daily_slack


def _payload(**over):
    p = {
        "report_date": "2026-10-08",
        "new_count": 0, "surge_count": 1, "regression_count": 0,
        "tldr": {
            "severity": "yellow",
            "platforms": [
                {"platform_label": "🍎 iOS", "crash_users": 727, "user_delta_pct": -3.1,
                 "new_count": 0, "status": "yellow"},
                {"platform_label": "📱 Android", "crash_users": 129, "user_delta_pct": -18.4,
                 "new_count": 0, "status": "unknown"},
            ],
            "must_see": {"title": "[String] AppHang", "platform": "🍎 iOS", "events": 984,
                         "delta_pct": 22.7, "url": "http://h/crashguard?issue=x", "is_new": False},
            "anomaly_total": 2,
            "total_users": 54022, "crashed_users": 856,
            "base_total_users": 51009, "base_crashed_users": 908,
        },
        "headline": "🟡 **关注** —— 今日全平台 fatal crash-free **98.42%**，建议工程师跟进",
        "crash_free_detail": {
            "all_versions": {"platforms": {
                "IOS": {"crash_free_users_pct": 98.2475, "crashed_users": 727, "total_users": 41484},
                "ANDROID": {"crash_free_users_pct": 98.9711, "crashed_users": 129, "total_users": 12538},
            }},
            "top_user_versions": {"platforms": {
                "IOS": {"version": "3.29.1-739"}, "ANDROID": {"version": "3.29.1-739"},
            }},
            "latest_versions": {"platforms": {
                "IOS": {"version": "4.0.302-1203", "crash_free_users_pct": 85.7708, "total_users": 253},
                "ANDROID": {"version": "3.29.1-739", "crash_free_users_pct": 99.0, "total_users": 11823},
            }},
        },
        "dual_window": {"platforms": {
            "IOS": {"today_fatal": 1150, "baseline_fatal": 959, "fatal_delta_pct": 19.9},
            "ANDROID": {"today_fatal": 247, "baseline_fatal": 316, "fatal_delta_pct": -21.8},
        }},
    }
    p.update(over)
    return p


def _render(payload=None, **kw):
    return build_daily_slack(report_type=kw.pop("report_type", "morning"),
                             target_date="2026-10-08", payload=payload or _payload(),
                             frontend_base_url="http://h/", **kw)


def _all_text(r) -> str:
    return json.dumps(r.payload, ensure_ascii=False)


def test_single_message_no_thread_and_title_only_in_text():
    r = _render()
    assert r.folds == ()
    assert r.color == "#ECB22E"
    assert "昨日复盘" in r.text
    assert "昨日复盘" not in _all_text(r)
    assert all(b["type"] != "header" for b in r.payload)


def test_headline_numbers_without_boilerplate():
    r = _render()
    head = r.payload[0]["text"]["text"]
    assert "98.42%" in head and "+0.2pp" in head and "856 人受影响" in head
    assert "建议工程师跟进" not in _all_text(r)
    assert "数据口径" not in _all_text(r)


def test_focus_and_must_see_link_keeps_brackets():
    r = _render()
    focus = r.payload[1]["text"]["text"]
    assert "🍎 iOS 727 人（-3%） 🟡" in focus
    assert "📱 Android 129 人（-18%）" in focus
    assert "<http://h/crashguard?issue=x|[String] AppHang>" in focus
    assert "+23% vs 上周" in focus


def test_must_see_omitted_when_absent():
    p = _payload()
    p["tldr"]["must_see"] = None
    assert "必看" not in _all_text(_render(p))


def test_crash_free_two_columns_with_latest_build_only_when_different():
    r = _render()
    cf = next(b for b in r.payload if "fields" in b)
    ios, android = (f["text"] for f in cf["fields"])
    assert "98.25%" in ios and "727 / 41,484" in ios and "+20% vs 上周" in ios
    assert "4.0.302-1203" in ios and "85.77%" in ios
    # Android 最新版就是主要版本 → 不重复列
    assert "新版" not in android
    # 上周 316 < 500 → 不给百分比
    assert "基数小" in android


def test_report_link_deep_links_by_type_and_date():
    r = _render(report_type="evening")
    last = r.payload[-1]
    assert last["type"] == "context"
    assert "http://h/crashguard/reports?type=evening&date=2026-10-08|查看完整速报" in last["elements"][0]["text"]
    assert "速报" in r.text


def test_coreguard_hint_prefixes_headline():
    r = _render(coreguard_section={"available": True, "headline_hint": "⚠️ 转写成功率持续偏低"})
    assert r.payload[0]["text"]["text"].startswith("⚠️ 转写成功率持续偏低；")


def test_falls_back_to_payload_headline_without_user_data_and_keeps_color():
    p = _payload()
    p["tldr"] = {"severity": "weird"}
    p.pop("crash_free_detail")
    r = _render(p)
    assert "*关注*" in r.payload[0]["text"]["text"]
    assert r.color  # 没有色条 Slack 不显示 text，标题会丢
    assert not any("fields" in b for b in r.payload)
