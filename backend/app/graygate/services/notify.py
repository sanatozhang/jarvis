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

from app.services.im import NotifyTarget, Rendered, dual_send, effective_provider, resolve_transport

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


def _raw_provider():
    return _settings().notify_provider


def _provider() -> str:
    """当前这一次发送用的单个渠道。`both` 在双发里解析成当前腿。"""
    return effective_provider((_settings().notify_provider or "feishu").strip().lower())


def report_target() -> NotifyTarget:
    """日报/变更通知的去处：群 or 频道。"""
    s = _settings()
    provider = _provider()
    if provider == "slack":
        return NotifyTarget(provider="slack", channel=s.slack_channel)
    return NotifyTarget(provider="feishu", channel=s.feishu_chat_id)


def alert_target() -> NotifyTarget:
    """运维告警（构建失败 / 调度停跑）的去处：**点对点**，不吵群。

    两个渠道下都用 `alert_email` 寻址 —— 邮箱在飞书是直接可寻址的，在 Slack
    走 `users.lookupByEmail`。所以这里不需要一个并行的 `alert_slack_user` 配置。
    """
    s = _settings()
    provider = _provider()
    return NotifyTarget(provider=provider, email=s.alert_email)


async def send_daily_report(target_date: date) -> Optional[bool]:
    """发一次日报。

    返回 `None` 表示**没有数据可报**（两平台版本枚举都空），这跟"发送失败"
    是两回事——调用方要把它记成 `available=False` 而不是 `degraded`。

    取数只跑一次（provider=both 时两条腿共用同一份 `GraygateReportData`，
    以前每条腿各查一遍 Datadog），发送前先把网页端完整版落库缓存——Slack 的
    「查看完整日报 →」读的就是它。缓存写失败不影响发送（`save_report` 自己兜底）。
    """
    from app.graygate.services.card_builder import collect_report_data
    from app.graygate.services.report_store import save_report

    data = await collect_report_data(target_date)
    if data is None:
        return None
    await save_report(data)
    return await _send_report_data(data)


@dual_send(_raw_provider)
async def _send_report_data(data) -> bool:
    target = report_target()
    transport = resolve_transport(target.provider)

    if target.provider == "slack":
        from app.graygate.services.slack_report import assemble_slack_message, report_url_for

        msg = assemble_slack_message(data, report_url_for(data.target_date))
    else:
        from app.graygate.services.card_builder import assemble_feishu_card

        msg = Rendered(payload=assemble_feishu_card(data))

    return await transport.send(target, msg)


@dual_send(_raw_provider)
async def send_focus_change(platform: str, action: str,
                            old_note: str, operator: str, *,
                            old_value: str = "", new_value: str = "") -> bool:
    """「主要版本」变更通知。"""
    target = report_target()
    transport = resolve_transport(target.provider)

    if target.provider == "slack":
        from app.graygate.services.slack_report import assemble_focus_change_message

        # Slack 是英文版，从原始值重新渲染（action / old_note 是给飞书的中文文案）
        msg = assemble_focus_change_message(platform, old_value, new_value, operator)
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


@dual_send(_raw_provider)
async def send_ops_alert(text: str, text_en: str = "") -> bool:
    """运维告警私聊。纯文本，两个渠道都不需要卡片。Slack 发 `text_en`（英文）。"""
    target = alert_target()
    body = (text_en or text) if target.provider == "slack" else text
    return await resolve_transport(target.provider).send_text(target, body)
