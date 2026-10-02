"""系统私聊通知的统一出口 —— 发版通知 / modulehub / DB 健康告警 / 站内反馈。

这几处不属于 crashguard / coreguard / graygate 任何一个模块，原来各自直接
`from app.services.feishu_cli import send_message`，于是模块粒度的渠道开关
管不到它们：三个模块切到 Slack 之后，这几条私聊还在悄悄走飞书。

**所有非模块的点对点发送都走这里**，不要再直接 import `feishu_cli`。

渠道由 `Settings.system_notify_provider` 决定（设置页「system」那一行，
见 `services/notify_switch.py`）。目标一律是**邮箱**：飞书直接按邮箱寻址，
Slack 走 `users.lookupByEmail`，所以不需要一套并行的 Slack 收件人配置。
"""
from __future__ import annotations

import logging

from app.services.im import NotifyTarget, resolve_transport

logger = logging.getLogger("jarvis.system_notify")


def provider() -> str:
    """当前渠道名。**永远返回一个已实现的 provider**。

    跟各模块 `notify.provider()` 同一个分工：启动期（`main.py`）配错了就
    fail-fast；运行期走到非法值时回落默认渠道 + 记 error，而不是抛异常把
    这条通知丢掉——DB 健康告警本身可能正在报一个线上故障。
    """
    from app.config import get_settings
    from app.services.im import DEFAULT_PROVIDER, implemented_providers

    raw = getattr(get_settings(), "system_notify_provider", None)
    if not isinstance(raw, str) or not raw.strip():
        return DEFAULT_PROVIDER
    name = raw.strip().lower()
    if name not in implemented_providers():
        logger.error("system_notify_provider=%r 不是已实现的渠道（%s），本次按 %s 发送",
                     raw, implemented_providers(), DEFAULT_PROVIDER)
        return DEFAULT_PROVIDER
    return name


async def send_text(email: str, text: str) -> bool:
    """给一个人发纯文本私聊。返回是否发出；**不抛**（调用方都是 best-effort）。"""
    if not (email or "").strip():
        logger.warning("system_notify.send_text: 收件邮箱为空，跳过")
        return False
    prov = provider()
    try:
        return bool(await resolve_transport(prov).send_text(
            NotifyTarget(provider=prov, email=email.strip()), text))
    except Exception as e:
        logger.warning("system_notify.send_text(%s, via %s) 失败: %s", email, prov, e)
        return False


async def send_image(email: str, content: bytes, filename: str = "screenshot.png") -> bool:
    """给一个人私聊发一张图片。返回是否发出；**不抛**。

    两个渠道的形态差得多，所以不进 `IMTransport` 契约（那里只有三个模块在用
    的卡片/文本两种消息）：飞书是「上传拿 image_key → 发 image 消息」，Slack
    是「邮箱 → uid → 开 DM 拿 D... → 三步上传」——`files.completeUploadExternal`
    拒收 `U...`，必须先 `conversations.open`。
    """
    if not (email or "").strip() or not content:
        return False
    prov = provider()
    try:
        if prov == "slack":
            from app.services import slack_cli

            uid = await slack_cli.uid_for_email(email.strip())
            if not uid:
                logger.error("system_notify.send_image: 邮箱 %s 在 Slack 里查不到对应用户", email)
                return False
            dm = await slack_cli.open_dm(uid)
            if not dm:
                return False
            return bool(await slack_cli.upload_file(dm, content, filename))

        from app.services import feishu_cli

        image_key = await feishu_cli.upload_image(content)
        return bool(await feishu_cli.send_image_message(image_key=image_key, email=email.strip()))
    except Exception as e:
        logger.warning("system_notify.send_image(%s, via %s) 失败: %s", email, prov, e)
        return False
