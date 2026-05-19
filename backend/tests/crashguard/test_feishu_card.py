"""飞书卡片构造器测试"""
from __future__ import annotations


def test_build_daily_card_anomaly_red_template():
    from app.crashguard.services.feishu_card import build_daily_card
    card = build_daily_card(
        report_type="morning",
        target_date="2026-04-29",
        markdown="# Test\n\n## 📱 Android\n\n- foo",
        payload={"new_count": 2, "surge_count": 1, "regression_count": 0},
        frontend_base_url="https://example.com",
    )
    assert card["header"]["template"] == "red"
    assert "🌅" in card["header"]["title"]["content"]
    assert "2026-04-29" in card["header"]["title"]["content"]


def test_build_daily_card_no_anomaly_turquoise_template():
    from app.crashguard.services.feishu_card import build_daily_card
    card = build_daily_card(
        report_type="evening",
        target_date="2026-04-29",
        markdown="# Test",
        payload={"new_count": 0, "surge_count": 0, "regression_count": 0},
    )
    assert card["header"]["template"] == "turquoise"
    assert "🌇" in card["header"]["title"]["content"]
    assert "速报" in card["header"]["title"]["content"]
    # v2 重构后：TL;DR 顶置，scope banner 紧随其后；老 payload 无 tldr 走 fallback 摘要
    import json
    rendered = json.dumps(card, ensure_ascii=False)
    assert "数据口径" in rendered      # scope banner 仍在卡片里
    assert "数据平稳" in rendered      # 无异常 fallback 文案保留


def test_build_daily_card_has_action_button():
    from app.crashguard.services.feishu_card import build_daily_card
    card = build_daily_card(
        report_type="morning",
        target_date="2026-04-29",
        markdown="# Test",
        payload={},
        frontend_base_url="https://example.com",
    )
    # v2 schema: button 直接作为 element，behaviors[0].default_url 取链接
    last = card["body"]["elements"][-1]
    assert last["tag"] == "button"
    assert last["behaviors"][0]["default_url"].startswith("https://example.com/crashguard")


def test_split_sections():
    from app.crashguard.services.feishu_card import _split_sections
    md = "intro line\n## A\nbody A\n## B\nbody B"
    sections = _split_sections(md)
    titles = [s["title"] for s in sections]
    assert "A" in titles
    assert "B" in titles


def test_card_truncates_long_section():
    """单段 lark_md 超 3500 char 自动截断（v2 后内容可能进 collapsible_panel）"""
    import json
    from app.crashguard.services.feishu_card import build_daily_card
    long_md = "## Section\n" + ("x" * 5000)
    card = build_daily_card(
        report_type="morning",
        target_date="2026-04-29",
        markdown=long_md,
        payload={},
    )
    # v2 后 "Section" 这种非关注关键字段会进 collapsible_panel，截断标记应该
    # 在 panel.elements[].text.content 里；用 full json 检查最简单
    rendered = json.dumps(card, ensure_ascii=False)
    assert "已截断" in rendered


def test_build_hourly_alert_card_with_new_version_section():
    """卡片包含 [新版本] 标签段 + first_seen 行"""
    from datetime import datetime
    import json
    from app.crashguard.services.feishu_card import build_hourly_alert_card
    card = build_hourly_alert_card(
        hour_utc=datetime(2026, 5, 14, 3, 0),
        new_items=[],
        surge_items=[],
        new_version_items=[{
            "issue_id": "x1", "title": "NewVer", "platform": "android",
            "version": "3.20.0", "first_seen_version": "3.20.0",
            "events_h": 50, "sessions_h": 800, "user_rate_pct": 0.65,
        }],
        new_crash_items=[],
        threshold_pct=10, frontend_base_url="http://x",
    )
    rendered = json.dumps(card, ensure_ascii=False)
    assert "新版本" in rendered
    assert "3.20.0" in rendered
    assert "0.65" in rendered    # user_rate_pct rendered


def test_build_daily_card_tldr_top_block():
    """新 TL;DR 顶置 + 必看 issue + 折叠区结构验证"""
    import json
    from app.crashguard.services.feishu_card import build_daily_card

    payload = {
        "new_count": 2, "surge_count": 1, "regression_count": 0,
        "tldr": {
            "severity": "red",
            "platforms": [
                {"platform_label": "📱 Android", "delta_pct": 73.0,
                 "new_count": 2, "surge_count": 1, "status": "red",
                 "today_fatal": 8902, "baseline_fatal": 5140},
                {"platform_label": "🍎 iOS", "delta_pct": 2.0,
                 "new_count": 0, "surge_count": 0, "status": "green",
                 "today_fatal": 1203, "baseline_fatal": 1180},
            ],
            "must_see": {
                "issue_id": "abc",
                "title": "FlutterMethodCall NPE",
                "platform": "📱 Android",
                "events": 3421,
                "delta_pct": 180.0,
                "url": "https://example.com/crashguard?issue=abc",
                "is_new": False,
            },
            "other_count": 14,
            "anomaly_total": 3,
        },
    }
    md = (
        "# Crashguard 日报 — 2026-05-14\n"
        "## ✨ 今日关注点\n- bullet 1\n"
        "## 📱 Android\n- detail Android\n"
        "## 🍎 iOS\n- detail iOS\n"
    )
    card = build_daily_card(
        report_type="morning",
        target_date="2026-05-14",
        markdown=md,
        payload=payload,
        frontend_base_url="https://example.com",
    )
    assert card["header"]["template"] == "red"
    rendered = json.dumps(card, ensure_ascii=False)
    # TL;DR 三行
    assert "今日重点" in rendered
    assert "+73%" in rendered            # Android fatal Δ
    assert "🆕2" in rendered              # 新增 chip
    assert "必看" in rendered
    assert "FlutterMethodCall NPE" in rendered
    assert "3,421 events" in rendered
    # 原"其他 **N** 个 issue 量级在基线范围内，无需立刻动"提示已迁移至
    # docs/crashguard/metrics-glossary.md，早晚报不再渲染此行。
    assert "无需立刻动" not in rendered
    assert "其他 **14**" not in rendered
    # 关注点段不折叠（关键字"关注"命中 EXPANDED_KEYWORDS）
    assert any(
        e.get("tag") == "div" and "今日关注点" in e.get("text", {}).get("content", "")
        for e in card["body"]["elements"]
    )
    # 平台详情走 collapsible_panel
    panels = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
    panel_titles = [p["header"]["title"]["content"] for p in panels]
    assert any("📱 Android" in t for t in panel_titles)
    assert any("🍎 iOS" in t for t in panel_titles)
    # 顶层 schema 2.0 + body.elements 镜像
    assert card.get("schema") == "2.0"
    assert "elements" in card.get("body", {})


def test_build_hourly_alert_card_with_new_crash_section():
    from datetime import datetime
    import json
    from app.crashguard.services.feishu_card import build_hourly_alert_card
    card = build_hourly_alert_card(
        hour_utc=datetime(2026, 5, 14, 3, 0),
        new_items=[], surge_items=[],
        new_version_items=[],
        new_crash_items=[{
            "issue_id": "y1", "title": "NewCrash", "platform": "ios",
            "first_seen_version": "3.18.0",
            "first_seen_at": "2026-05-10T00:00:00",
            "events_24h": 200, "sessions_24h": 400,
        }],
        threshold_pct=10, frontend_base_url="http://x",
    )
    rendered = json.dumps(card, ensure_ascii=False)
    assert "新 crash" in rendered or "新crash" in rendered    # tolerate space variations
    assert "200" in rendered
    assert "3.18.0" in rendered
