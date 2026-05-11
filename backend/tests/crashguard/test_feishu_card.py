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
    # 平稳措辞
    summary_div = card["elements"][0]
    assert "数据平稳" in summary_div["text"]["content"]


def test_build_daily_card_has_action_button():
    from app.crashguard.services.feishu_card import build_daily_card
    card = build_daily_card(
        report_type="morning",
        target_date="2026-04-29",
        markdown="# Test",
        payload={},
        frontend_base_url="https://example.com",
    )
    last = card["elements"][-1]
    assert last["tag"] == "action"
    assert last["actions"][0]["url"].startswith("https://example.com/crashguard")


def test_split_sections():
    from app.crashguard.services.feishu_card import _split_sections
    md = "intro line\n## A\nbody A\n## B\nbody B"
    sections = _split_sections(md)
    titles = [s["title"] for s in sections]
    assert "A" in titles
    assert "B" in titles


def test_card_truncates_long_section():
    """单段 lark_md 超 3500 char 自动截断"""
    from app.crashguard.services.feishu_card import build_daily_card
    long_md = "## Section\n" + ("x" * 5000)
    card = build_daily_card(
        report_type="morning",
        target_date="2026-04-29",
        markdown=long_md,
        payload={},
    )
    # 找 div 元素中包含截断标记
    contents = [
        e["text"]["content"]
        for e in card["elements"]
        if e.get("tag") == "div" and isinstance(e.get("text"), dict)
    ]
    assert any("已截断" in c for c in contents)
