"""Graygate 的通知出口 —— 解析 target、选 transport、按 provider 渲染。

**所有 graygate 的发送都走这里**，不要再直接 import `feishu_cli`。
五个发送点（scheduler 的日报 / 失败告警 / 停跑告警、api 的手动触发、
focus_version 的变更通知）全部收口到下面三个函数。

依赖方向：graygate → services/im → services/{feishu_cli,slack_cli}。
`services/im` 不认识 graygate，所以 target 在这里解析（见 `im/base.py` 的
模块 docstring）。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from app.services.im import NotifyTarget, Rendered, resolve_transport

logger = logging.getLogger("jarvis.graygate.notify")


def _settings():
    """⚠️ **延迟 import，不要改成模块级 `from ... import get_graygate_settings`。**

    测试普遍按模块属性 patch（`monkeypatch.setattr("app.graygate.config.
    get_graygate_settings", ...)`），模块级 import 是早绑定，那种 patch 碰不到
    这里——表现是测试里拿到**本机 `.env` 里真实的 `GRAYGATE_FEISHU_CHAT_ID`**，
    也就是真实的灰度群。`tests/conftest.py` 顶部记的 2026-08-23 事故就是这个形状。
    """
    from app.graygate.config import get_graygate_settings

    return get_graygate_settings()


def report_target() -> NotifyTarget:
    """日报/变更通知的去处：群 or 频道。"""
    s = _settings()
    provider = (s.notify_provider or "feishu").strip().lower()
    if provider == "slack":
        return NotifyTarget(provider="slack", channel=s.slack_channel)
    return NotifyTarget(provider="feishu", channel=s.feishu_chat_id)


def alert_target() -> NotifyTarget:
    """运维告警（构建失败 / 调度停跑）的去处：**点对点**，不吵群。

    两个渠道下都用 `alert_email` 寻址 —— 邮箱在飞书是直接可寻址的，在 Slack
    走 `users.lookupByEmail`。所以这里不需要一个并行的 `alert_slack_user` 配置。
    """
    s = _settings()
    provider = (s.notify_provider or "feishu").strip().lower()
    return NotifyTarget(provider=provider, email=s.alert_email)


async def send_daily_report(target_date: date) -> Optional[bool]:
    """发一次日报。

    返回 `None` 表示**没有数据可报**（两平台版本枚举都空），这跟"发送失败"
    是两回事——调用方要把它记成 `available=False` 而不是 `degraded`。
    """
    target = report_target()
    transport = resolve_transport(target.provider)

    if target.provider == "slack":
        from app.graygate.services.slack_report import build_report_message

        msg = await build_report_message(target_date)
        if msg is None:
            return None
    else:
        from app.graygate.services.card_builder import build_report_card

        report = await build_report_card(target_date)
        if not report.available:
            return None
        msg = Rendered(payload=report.card)

    return await transport.send(target, msg)


async def send_focus_change(platform: str, action: str,
                            old_note: str, operator: str) -> bool:
    """「主要版本」变更通知。"""
    target = report_target()
    transport = resolve_transport(target.provider)

    if target.provider == "slack":
        from app.graygate.services.slack_report import assemble_focus_change_message

        msg = assemble_focus_change_message(platform, action, old_note, operator)
    else:
        msg = Rendered(payload={
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text",
                          "content": f"🔖 4.0.3 灰度「主要版本」变更 · {platform.upper()}"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": f"**{platform.upper()}** {action}"}},
                {"tag": "div", "text": {"tag": "lark_md", "content": old_note}},
                {"tag": "div", "text": {"tag": "lark_md", "content": f"操作人：{operator}"}},
            ],
        })
    return await transport.send(target, msg)


async def send_ops_alert(text: str) -> bool:
    """运维告警私聊。纯文本，两个渠道都不需要卡片。"""
    target = alert_target()
    return await resolve_transport(target.provider).send_text(target, text)
