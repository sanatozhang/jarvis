"""graygate 的 Slack 渲染 + 通知出口单测。

三类断言：
1. **飞书和 Slack 消费同一份数据**——同一个 `GraygateReportData` 渲染出来的
   两边，严重度判定必须一致（红/绿不能各判各的）
2. **Block Kit 上限**——超了是静默截断或 invalid_blocks，不是温和降级
3. **provider 路由**——切到 slack 之后不能还在调飞书
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.graygate.services import notify as gnotify
from app.graygate.services.card_builder import GraygateReportData, assemble_feishu_card
from app.graygate.services.slack_report import (
    assemble_focus_change_message,
    assemble_slack_message,
)
from app.services.im.mrkdwn import MAX_BLOCKS, MAX_FIELD_TEXT, lark_md_to_mrkdwn


def _data(*, worsen=False, new_crash=False, top=False) -> GraygateReportData:
    return GraygateReportData(
        target_date=date(2026, 8, 18),
        d1_day=date(2026, 8, 17),
        version_pattern="4.0.3*",
        worsen_lines=["- **🍎 iOS** 大盘 crash-free 掉了 0.3%"] if worsen else [],
        columns_md=["**🍎 iOS**\n\n__大盘（4.0.3*）__ ok",
                    "**🤖 Android**\n\n__大盘（4.0.3*）__ ok"],
        new_crash_md="**🆕 新增崩溃**\n- [Foo.crash →](https://dd/x)" if new_crash else None,
        top_crash_md="**🔥 Top5 崩溃**\n- Bar" if top else None,
        top_jank_md="**🟠 Top5 卡顿**\n- Baz" if top else None,
    )


# ---------------------------------------------------------------------------
# 语法转换
# ---------------------------------------------------------------------------
def test_link_is_converted_before_bold():
    """链接必须先转。

    text 里含 `**` 的链接（`[**崩溃** →](url)`）如果先转加粗，`](` 之间的内容
    也会被动过，链接正则就匹配不上了——结果是界面上出现裸的 markdown 语法。
    """
    assert lark_md_to_mrkdwn("[**崩溃** →](https://x)") == "<https://x|*崩溃* →>"


def test_underline_becomes_italic():
    assert lark_md_to_mrkdwn("__主要版本__") == "_主要版本_"


# ---------------------------------------------------------------------------
# 两个渲染器消费同一份数据
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs,expect_red", [
    ({}, False),
    ({"worsen": True}, True),
    ({"new_crash": True}, True),
    ({"top": True}, False),          # Top 榜天天有，不构成"红"
])
def test_severity_matches_between_feishu_and_slack(kwargs, expect_red):
    """红/绿判定必须两边一致，判据都是 `GraygateReportData.is_red`。

    各判各的后果很隐蔽：切渠道之后同一天的报告在飞书是红的、在 Slack 是绿的，
    没人会立刻发现，但"看颜色判断今天要不要看"这个习惯就废了。
    """
    data = _data(**kwargs)
    assert data.is_red is expect_red
    feishu_red = assemble_feishu_card(data)["header"]["template"] == "red"
    slack_red = assemble_slack_message(data).color == "#E01E5A"
    assert feishu_red is expect_red
    assert slack_red is expect_red


def test_metrics_table_stays_in_main_message():
    """指标双列留在主消息，**不能**塞进 thread。

    graygate 的日报本身就是这张表；塞进 thread 的话，没有恶化的那天主消息
    会变成一条没有内容的壳。折叠段只放附录性质的崩溃/卡顿榜。
    """
    msg = assemble_slack_message(_data(new_crash=True, top=True))
    body = json.dumps(msg.payload, ensure_ascii=False)
    assert "iOS" in body and "Android" in body
    fold_titles = [f.title for f in msg.folds]
    assert fold_titles == ["🆕 新增崩溃堆栈", "🔥 Top 崩溃 / 卡顿"]


def test_no_optional_sections_means_no_folds():
    assert assemble_slack_message(_data()).folds == ()


def test_notification_text_says_how_bad_it_is():
    """锁屏推送上只看得到 text。每天长一个样的话很快就被无视了。"""
    assert "无恶化" in assemble_slack_message(_data()).text
    assert "🔴 1 项恶化" in assemble_slack_message(_data(worsen=True)).text


# ---------------------------------------------------------------------------
# Block Kit 上限
# ---------------------------------------------------------------------------
def test_main_message_respects_block_kit_limits():
    msg = assemble_slack_message(_data(worsen=True, new_crash=True, top=True))
    assert len(msg.payload) <= MAX_BLOCKS
    for fold in msg.folds:
        assert len(fold.blocks) <= MAX_BLOCKS


def test_oversized_column_is_clipped_with_a_visible_marker():
    """单个 field 超 2000 字会被 Slack 截断。截断必须留痕——不留的话
    "内容被截断"和"内容本来就这么短"在界面上分不出来，而前者是要改代码的 bug。"""
    data = _data()
    data.columns_md = ["x" * 5000, "y"]
    fields = assemble_slack_message(data).payload[-1]["fields"]
    assert len(fields[0]["text"]) <= MAX_FIELD_TEXT
    assert "已截断" in fields[0]["text"]


def test_focus_change_message_has_blue_color_like_feishu():
    msg = assemble_focus_change_message("ios", "设为 `4.0.3.1`", "原值：未设置", "sanato")
    assert msg.color == "#1D9BD1"
    assert "IOS" in msg.payload[0]["text"]["text"]


# ---------------------------------------------------------------------------
# provider 路由
# ---------------------------------------------------------------------------
def _settings(provider="feishu", slack_channel="C_gray", feishu_chat_id="oc_gray"):
    return SimpleNamespace(
        notify_provider=provider, slack_channel=slack_channel,
        feishu_chat_id=feishu_chat_id, alert_email="ops@plaud.ai",
        feishu_enabled=True, send_enabled=True,
    )


@pytest.mark.parametrize("provider,expected_channel", [
    ("feishu", "oc_gray"),
    ("slack", "C_gray"),
])
def test_report_target_follows_provider(provider, expected_channel):
    with patch.object(gnotify, "_settings", return_value=_settings(provider)):
        t = gnotify.report_target()
    assert t.provider == provider
    assert t.channel == expected_channel


def test_alert_target_uses_email_on_both_providers():
    """运维告警两边都用邮箱寻址——所以不需要一个并行的 alert_slack_user 配置。"""
    for provider in ("feishu", "slack"):
        with patch.object(gnotify, "_settings", return_value=_settings(provider)):
            t = gnotify.alert_target()
        assert t.provider == provider and t.email == "ops@plaud.ai"


@pytest.mark.asyncio
async def test_slack_provider_does_not_touch_feishu():
    """切到 slack 之后一次飞书调用都不该有。"""
    feishu = AsyncMock(return_value=True)
    with patch.object(gnotify, "_settings", return_value=_settings("slack")), \
         patch("app.services.feishu_cli.send_interactive_card", feishu), \
         patch("app.graygate.services.slack_report.build_report_message",
               AsyncMock(return_value=assemble_slack_message(_data()))), \
         patch("app.services.slack_cli.post_message",
               AsyncMock(return_value="1790.1")) as post:
        ok = await gnotify.send_daily_report(date(2026, 8, 18))
    assert ok is True
    feishu.assert_not_awaited()
    assert post.await_args_list[0].args[0] == "C_gray"


@pytest.mark.asyncio
async def test_no_data_returns_none_not_false():
    """"没有数据可报"和"发送失败"必须分得开：前者 status=success，
    后者 status=degraded 且要私聊告警。"""
    with patch.object(gnotify, "_settings", return_value=_settings("slack")), \
         patch("app.graygate.services.slack_report.build_report_message",
               AsyncMock(return_value=None)):
        assert await gnotify.send_daily_report(date(2026, 8, 18)) is None
