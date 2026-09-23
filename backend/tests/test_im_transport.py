"""IM 传输层（`app/services/im/`）单测。

这里测的是**契约和降级行为**，不是"能不能发出去"——真实发送在真机验证里走。
重点覆盖三件迁移期最容易静默出错的事：

1. 未知 provider 必须抛异常，不能回落飞书（回落会让"配错了"表现成"一切正常"）
2. target 没配时必须返回 False + 留日志，不能返回 True
3. thread 回复失败**不能**把主消息的成功吃掉（否则发送锁不落 sent → 重发）
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.im import (
    Fold,
    NotifyTarget,
    Rendered,
    implemented_providers,
    resolve_transport,
)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
def test_implemented_providers_is_the_single_source_of_truth():
    assert implemented_providers() == ("feishu", "slack")


def test_resolve_transport_is_case_insensitive():
    assert resolve_transport("SLACK").provider == "slack"
    assert resolve_transport("  Feishu ").provider == "feishu"


def test_unknown_provider_raises_instead_of_falling_back():
    """**不能**回落到飞书。

    回落的后果不是报错而是：配置写错了、启动没报错、消息全发去了另一个渠道，
    而发送方看起来一切正常。整个迁移里最贵的一类 bug 就是这个形状。
    """
    with pytest.raises(ValueError) as e:
        resolve_transport("wechat")
    assert "wechat" in str(e.value)
    # 异常里要带上"那什么才是对的"，否则排查还得去翻代码
    assert "feishu" in str(e.value) and "slack" in str(e.value)


# ---------------------------------------------------------------------------
# NotifyTarget
# ---------------------------------------------------------------------------
def test_target_configured_requires_channel_or_email():
    assert not NotifyTarget(provider="slack").configured
    assert NotifyTarget(provider="slack", channel="C1").configured
    assert NotifyTarget(provider="feishu", email="a@plaud.ai").configured


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["feishu", "slack"])
async def test_unconfigured_target_returns_false_not_true(provider):
    """没配 target 时必须是 False。

    返回 True 的话，上层会把"压根没发"记成"发送成功"——graygate 的心跳会显示
    success、crashguard 的发送锁会落 "sent"，然后没有任何人收到消息、也没有
    任何地方留下痕迹。
    """
    t = resolve_transport(provider)
    empty = NotifyTarget(provider=provider)
    assert await t.send(empty, Rendered(payload={})) is False
    assert await t.send_text(empty, "hi") is False


# ---------------------------------------------------------------------------
# FeishuTransport
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_feishu_prefers_channel_over_email():
    send = AsyncMock(return_value=True)
    with patch("app.services.feishu_cli.send_interactive_card", send):
        ok = await resolve_transport("feishu").send(
            NotifyTarget(provider="feishu", channel="oc_x", email="a@plaud.ai"),
            Rendered(payload={"schema": "2.0"}),
        )
    assert ok is True
    assert send.await_args.kwargs["chat_id"] == "oc_x"
    assert "email" not in send.await_args.kwargs


@pytest.mark.asyncio
async def test_feishu_falls_back_to_email_when_no_channel():
    send = AsyncMock(return_value=True)
    with patch("app.services.feishu_cli.send_interactive_card", send):
        await resolve_transport("feishu").send(
            NotifyTarget(provider="feishu", email="a@plaud.ai"),
            Rendered(payload={"schema": "2.0"}),
        )
    assert send.await_args.kwargs["email"] == "a@plaud.ai"


@pytest.mark.asyncio
async def test_feishu_ignores_folds():
    """飞书的折叠区是各模块 builder 直接编译进 card 的 collapsible_panel，
    不经过 transport。传了 folds 也不该产生额外的消息。"""
    send = AsyncMock(return_value=True)
    with patch("app.services.feishu_cli.send_interactive_card", send):
        await resolve_transport("feishu").send(
            NotifyTarget(provider="feishu", channel="oc_x"),
            Rendered(payload={"schema": "2.0"},
                     folds=(Fold(title="FYI", blocks=[{"type": "divider"}]),)),
        )
    send.assert_awaited_once()   # 只有主消息，没有第二条


# ---------------------------------------------------------------------------
# SlackTransport
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slack_posts_main_then_each_fold_in_thread():
    post = AsyncMock(return_value="1790000000.0001")
    with patch("app.services.slack_cli.post_message", post):
        ok = await resolve_transport("slack").send(
            NotifyTarget(provider="slack", channel="C1"),
            Rendered(
                payload=[{"type": "divider"}],
                folds=(Fold(title="A", blocks=[{"type": "divider"}]),
                       Fold(title="B", blocks=[{"type": "divider"}])),
                text="标题", color="#E01E5A",
            ),
        )
    assert ok is True
    assert post.await_count == 3          # 主消息 + 2 条 thread 回复

    main = post.await_args_list[0]
    assert main.args[0] == "C1"
    assert main.kwargs["color"] == "#E01E5A"
    assert "thread_ts" not in main.kwargs  # 主消息不能自己挂 thread

    for call in post.await_args_list[1:]:
        assert call.kwargs["thread_ts"] == "1790000000.0001"
        # 折叠段不该带色条——色条挂在主消息上，每条回复都挂会糊成一片
        assert not call.kwargs.get("color")


@pytest.mark.asyncio
async def test_slack_thread_failure_does_not_fail_the_whole_send():
    """**这条是防重发的**。

    crashguard 的 `feishu_message_id` 是跨实例发送锁：发送成功才落 "sent"。
    如果一条 FYI thread 回复失败就让 send 返回 False，那把锁不会落 "sent"，
    下一个实例会把整张早晚报**重发一遍**——用户看到同一份报告发两次，而真正
    的问题只是一条折叠段没发出去。
    """
    async def _post(channel, text="", **kw):
        if kw.get("thread_ts"):
            raise RuntimeError("thread boom")
        return "1790000000.0002"

    with patch("app.services.slack_cli.post_message", AsyncMock(side_effect=_post)):
        ok = await resolve_transport("slack").send(
            NotifyTarget(provider="slack", channel="C1"),
            Rendered(payload=[], folds=(Fold(title="A"),), text="t"),
        )
    assert ok is True


@pytest.mark.asyncio
async def test_slack_main_message_failure_is_false():
    with patch("app.services.slack_cli.post_message",
               AsyncMock(side_effect=RuntimeError("main boom"))):
        ok = await resolve_transport("slack").send(
            NotifyTarget(provider="slack", channel="C1"), Rendered(payload=[]),
        )
    assert ok is False


@pytest.mark.asyncio
async def test_slack_resolves_email_to_uid():
    post = AsyncMock(return_value="1790000000.0003")
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="U123")), \
         patch("app.services.slack_cli.post_message", post):
        ok = await resolve_transport("slack").send_text(
            NotifyTarget(provider="slack", email="a@plaud.ai"), "hi",
        )
    assert ok is True
    assert post.await_args.args[0] == "U123"


@pytest.mark.asyncio
async def test_slack_unresolvable_email_fails_loudly(caplog):
    """邮箱在 Slack 里查不到 → 返回 False **并且**打 error 日志。

    这种降级的表现是"告警一条都没来"，没有任何报错。不留日志的话，排查的人
    完全没有下手的地方。
    """
    with patch("app.services.slack_cli.uid_for_email", AsyncMock(return_value="")):
        ok = await resolve_transport("slack").send_text(
            NotifyTarget(provider="slack", email="ghost@plaud.ai"), "hi",
        )
    assert ok is False
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("ghost@plaud.ai" in m for m in errors), errors


# ---------------------------------------------------------------------------
# 启动期校验
# ---------------------------------------------------------------------------
def test_validate_provider_raises_on_unknown():
    """启动期 fail-fast。

    不校验的后果不是报错，而是：配置写了个 `slak`，服务正常起来，然后每一次
    告警在 resolve_transport 抛异常被上游 `except Exception` 吞掉——表现是
    "这个模块的告警悄悄没了"。
    """
    from app.services.im import validate_provider

    assert validate_provider("crashguard", "slack") == "slack"
    assert validate_provider("crashguard", "") == "feishu"   # 空 = 默认
    with pytest.raises(ValueError) as e:
        validate_provider("crashguard", "slak")
    assert "crashguard" in str(e.value) and "slak" in str(e.value)


def test_runtime_falls_back_instead_of_raising(caplog):
    """运行期跟启动期**刻意不同**：非法值回落默认渠道 + 记 error，不抛。

    运行期抛异常等于把这条告警丢掉，而告警本身可能正在报线上故障。
    """
    from types import SimpleNamespace

    from app.crashguard.services import notify

    assert notify.provider(SimpleNamespace(notify_provider="slak")) == "feishu"
    assert any("slak" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


def test_runtime_tolerates_mock_settings():
    """测试里的 settings 桩常常是 MagicMock，`notify_provider` 取出来不是字符串。
    不能让桩对象把告警路径炸掉。"""
    from unittest.mock import MagicMock

    from app.crashguard.services import notify

    assert notify.provider(MagicMock()) == "feishu"
