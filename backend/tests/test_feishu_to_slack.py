"""飞书 card → Slack Block Kit 编译器。

crashguard 的 9 张卡全部走这条路（graygate/coreguard 是手写渲染器，判据见
`feishu_to_slack` 的模块 docstring）。所以这里的覆盖要足够严——编译错了的
表现是"早晚报发出去了但版面烂掉"或者"整条发不出去"。
"""
from __future__ import annotations

import json

import pytest

from app.services.im.feishu_to_slack import compile_card
from app.services.im.mrkdwn import MAX_BLOCKS


def _div(md):
    return {"tag": "div", "text": {"tag": "lark_md", "content": md}}


def _card(elements, template="red", title="标题", v2=False):
    c = {"header": {"template": template,
                    "title": {"tag": "plain_text", "content": title}}}
    if v2:
        c["schema"] = "2.0"
        c["body"] = {"elements": elements}
    else:
        c["elements"] = elements
    return c


# ---------------------------------------------------------------------------
# 基本元素
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("v2", [False, True])
def test_reads_both_v1_elements_and_v2_body_elements(v2):
    """v1 把元素放在 `elements`，v2 放在 `body.elements`。

    crashguard 两种都有（早晚报是 v2，几个告警卡是 v1）——只认一种的话，
    另一种会编译成一张只有标题的空卡，而且不报错。
    """
    msg = compile_card(_card([_div("**hi**")], v2=v2))
    assert msg.payload[0]["type"] == "header"
    assert msg.payload[1]["text"]["text"] == "*hi*"


@pytest.mark.parametrize("template,color", [
    ("red", "#E01E5A"), ("green", "#2EB67D"), ("turquoise", "#2EB67D"),
    ("blue", "#1D9BD1"), ("yellow", "#ECB22E"),
])
def test_header_template_becomes_attachment_color(template, color):
    assert compile_card(_card([], template=template)).color == color


def test_unknown_template_gets_no_color_rather_than_a_guess():
    """颜色传达严重度，猜错比没有更糟。"""
    assert compile_card(_card([], template="wisteria")).color == ""


def test_hr_and_note_map_to_divider_and_context():
    msg = compile_card(_card([
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "lark_md", "content": "小字 **说明**"}]},
    ]))
    types = [b["type"] for b in msg.payload]
    assert types == ["header", "divider", "context"]
    assert msg.payload[2]["elements"][0]["text"] == "小字 *说明*"


# ---------------------------------------------------------------------------
# 按钮
# ---------------------------------------------------------------------------
def test_url_button_survives():
    msg = compile_card(_card([{"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": "打开看板"},
         "url": "https://app.datadoghq.com/x"},
    ]}]))
    btn = msg.payload[-1]["elements"][0]
    assert btn["url"] == "https://app.datadoghq.com/x"
    assert btn["text"]["text"] == "打开看板"


def test_bare_v2_button_with_behaviors_survives():
    """飞书 v2 允许 `button` 直接做 element，且 URL 藏在 `behaviors[].default_url`。

    crashguard 早晚报底部那个「在 Web 端查看 / 操作」就是这种写法。只认
    `action` + `url` 的话它会被当成回调按钮静默丢掉 —— 2026-09-23 真机验证
    时就是这么发现的（日志里一行 `未知元素 tag='button'`）。
    """
    msg = compile_card(_card([{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "📊 在 Web 端查看 / 操作"},
        "type": "primary",
        "behaviors": [{"type": "open_url",
                       "default_url": "https://jarvis.nicebuild.click/crashguard"}],
    }]))
    btn = msg.payload[-1]["elements"][0]
    assert btn["url"] == "https://jarvis.nicebuild.click/crashguard"


def test_multi_url_button_survives():
    msg = compile_card(_card([{"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": "打开"},
         "multi_url": {"url": "https://x.com/a", "pc_url": "https://x.com/pc"}},
    ]}]))
    assert msg.payload[-1]["elements"][0]["url"] == "https://x.com/a"


def test_callback_button_is_dropped_loudly(caplog):
    """没有 url 的按钮是回调按钮，Slack 侧需要 interactivity + HMAC 端点，
    本期不支持。丢掉可以，但必须留日志——静默丢会让人以为是渲染 bug。"""
    msg = compile_card(_card([{"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": "一键 approve"},
         "value": {"op": "approve"}},
    ]}]))
    assert all(b["type"] != "actions" for b in msg.payload)
    assert any("一键 approve" in r.getMessage() for r in caplog.records
               if r.levelname == "WARNING")


# ---------------------------------------------------------------------------
# column_set
# ---------------------------------------------------------------------------
def test_two_columns_become_section_fields():
    msg = compile_card(_card([{"tag": "column_set", "columns": [
        {"tag": "column", "elements": [_div("**iOS**\n99.6%")]},
        {"tag": "column", "elements": [_div("**Android**\n99.3%")]},
    ]}]))
    fields = msg.payload[-1]["fields"]
    assert len(fields) == 2
    assert "*iOS*" in fields[0]["text"] and "*Android*" in fields[1]["text"]


def test_three_columns_flatten_instead_of_being_squeezed():
    """Block Kit 的 fields 固定两列。>2 列挤进去会让每格都被截断，
    不如竖排。"""
    msg = compile_card(_card([{"tag": "column_set", "columns": [
        {"tag": "column", "elements": [_div(f"列 {i}")]} for i in range(3)
    ]}]))
    sections = [b for b in msg.payload if b["type"] == "section"]
    assert len(sections) == 3
    assert all("fields" not in b for b in sections)


# ---------------------------------------------------------------------------
# collapsible_panel → thread（整个迁移最关键的一条映射）
# ---------------------------------------------------------------------------
def _panel(title, elements, ):
    return {"tag": "collapsible_panel",
            "header": {"title": {"tag": "markdown", "content": title}},
            "elements": elements}


def test_collapsible_panel_becomes_a_thread_reply():
    msg = compile_card(_card([
        _div("TL;DR：今天有 2 个新增崩溃"),
        _panel("**双窗口对照**", [_div("24h vs 上周同日")]),
    ]))
    # 折叠段不留在主消息里
    body = json.dumps(msg.payload, ensure_ascii=False)
    assert "TL;DR" in body and "双窗口对照" not in body
    assert len(msg.folds) == 1
    fold = msg.folds[0]
    assert fold.title == "*双窗口对照*"
    # 折叠段自己带小标题，否则 thread 里只看到一堆数字
    assert "双窗口对照" in json.dumps(fold.blocks, ensure_ascii=False)
    assert "24h vs 上周同日" in json.dumps(fold.blocks, ensure_ascii=False)


def test_multiple_panels_keep_their_order():
    msg = compile_card(_card([
        _panel("A", [_div("a")]), _panel("B", [_div("b")]), _panel("C", [_div("c")]),
    ]))
    assert [f.title for f in msg.folds] == ["A", "B", "C"]


def test_nested_panels_are_flattened_to_one_level():
    """Slack 没有"thread 里的 thread"，嵌套折叠只能拍平到同一层。"""
    msg = compile_card(_card([
        _panel("外层", [_div("x"), _panel("内层", [_div("y")])]),
    ]))
    titles = [f.title for f in msg.folds]
    assert "内层" in titles and "外层" in titles


def test_panel_inside_column_is_still_collected():
    """飞书允许把折叠区嵌在 column 里；收集必须是递归的，否则那段内容
    会凭空消失。"""
    msg = compile_card(_card([{"tag": "column_set", "columns": [
        {"tag": "column", "elements": [_div("左"), _panel("藏在列里", [_div("z")])]},
        {"tag": "column", "elements": [_div("右")]},
    ]}]))
    assert [f.title for f in msg.folds] == ["藏在列里"]


# ---------------------------------------------------------------------------
# 上限
# ---------------------------------------------------------------------------
def test_too_many_blocks_overflow_into_thread_not_dropped():
    """撑爆 50 blocks = invalid_blocks = 整条消息发不出去。

    溢出的部分进 thread 而不是直接丢：丢掉的恰恰是排在最后的详情段。
    """
    msg = compile_card(_card([_div(f"第 {i} 段") for i in range(80)]))
    assert len(msg.payload) <= MAX_BLOCKS
    assert msg.folds and msg.folds[0].title == "（续）"
    assert "第 79 段" in json.dumps(msg.folds[0].blocks, ensure_ascii=False)


def test_unknown_tag_is_skipped_with_a_warning(caplog):
    msg = compile_card(_card([{"tag": "chart", "chart_spec": {}}, _div("正常段")]))
    assert any("chart" in r.getMessage() for r in caplog.records
               if r.levelname == "WARNING")
    assert any("正常段" in json.dumps(b, ensure_ascii=False) for b in msg.payload)


# ---------------------------------------------------------------------------
# 真卡片端到端
# ---------------------------------------------------------------------------
def test_real_crashguard_daily_card_compiles_within_limits():
    """拿真的 `build_daily_card()` 输出跑一遍。

    这是整个系统最常被读的一条消息，也是唯一用到 collapsible_panel 的地方。
    """
    from app.crashguard.services.feishu_card import build_daily_card

    card = build_daily_card(
        report_type="morning",
        target_date="2026-09-22",
        markdown="## 今日概览\n- 新增 2\n- 突增 1\n\n## 明细\n正文若干",
        payload={
            "new_count": 2, "surge_count": 1, "regression_count": 0,
            "data_window_hours": 24,
            "tldr": {"severity": "red", "headline": "Android 崩溃率上升"},
        },
        frontend_base_url="http://localhost:3000",
    )
    msg = compile_card(card)

    assert msg.payload[0]["type"] == "header"
    assert msg.color == "#E01E5A"
    assert len(msg.payload) <= MAX_BLOCKS
    # 底部那个 v2 裸 button 必须活下来（它是读者跳回 Web 端的唯一入口）
    actions = [b for b in msg.payload if b["type"] == "actions"]
    assert actions, "早晚报底部的 Web 端按钮被丢了"
    assert actions[-1]["elements"][0]["url"].endswith("/crashguard")
    for fold in msg.folds:
        assert len(fold.blocks) <= MAX_BLOCKS
    # 每个 section 的文本不能超 3000（超了 Slack 直接 invalid_blocks）
    for b in msg.payload:
        if b.get("type") == "section" and "text" in b:
            assert len(b["text"]["text"]) <= 3000
