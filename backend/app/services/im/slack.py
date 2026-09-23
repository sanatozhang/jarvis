"""SlackTransport —— `IMTransport` 的 Slack 实现。

只做投递。Block Kit 的构造在各模块自己的 `slack_*.py` 里。

两个跟飞书形态不同、必须在这一层兜住的地方：

1. **折叠段 → thread 回复。** Block Kit 没有折叠区；主消息承担"第一屏速读"，
   FYI 全部进 thread。层次跟飞书 `collapsible_panel` 同构。
2. **DM 要先把邮箱翻成 uid。** 飞书可以直接 `email=` 寻址，Slack 不行。
"""
from __future__ import annotations

import logging

from app.services.im.base import IMTransport, NotifyTarget, Rendered

logger = logging.getLogger("jarvis.services.im.slack")


class SlackTransport(IMTransport):
    provider = "slack"

    async def _resolve_channel(self, target: NotifyTarget) -> str:
        """把 NotifyTarget 落成一个真实的 Slack channel id。

        `channel` 直接用；否则邮箱 → uid。uid 可以直接当 channel 发（Slack
        接受 `U...`），不必先 `conversations.open`。
        """
        from app.services import slack_cli

        if target.channel:
            return target.channel
        if not target.email:
            return ""
        uid = await slack_cli.uid_for_email(target.email)
        if not uid:
            # 邮箱在 Slack 里查不到——通常是这个人还没加入工作区，或者配置里
            # 的邮箱跟 Slack profile 上的不一致。必须留信号：这种降级的表现
            # 是"告警一条都没来"，不会有任何报错。
            logger.error("SlackTransport: 邮箱 %s 在 Slack 里查不到对应用户，消息发不出去",
                         target.email)
        return uid

    async def send(self, target: NotifyTarget, msg: Rendered) -> bool:
        from app.services import slack_cli

        if not target.configured:
            logger.warning("SlackTransport.send: target 没配（channel 和 email 都空），跳过")
            return False
        channel = await self._resolve_channel(target)
        if not channel:
            return False

        try:
            ts = await slack_cli.post_message(
                channel, text=msg.text, blocks=msg.payload, color=msg.color,
            )
        except Exception as e:
            logger.error("SlackTransport.send 主消息失败 channel=%s: %s", channel, e)
            return False
        if not ts:
            return False

        # ------------------------------------------------------------------
        # thread 回复：串行 + 失败**不**影响返回值
        #
        # 这不是偷懒。crashguard 的 `feishu_message_id` 是一把跨实例发送锁：
        # 发送成功才落 "sent"。如果某条 FYI 回复失败就让整个 send 返回 False，
        # 那把锁不会落 "sent"，**下一个实例会把整张早晚报重发一遍** —— 用户
        # 看到的是同一份报告发两次，而真正的问题只是一条折叠段没发出去。
        #
        # 主消息成功 = 这次通知成立。折叠段是增量信息，失败记日志就够。
        # ------------------------------------------------------------------
        for fold in msg.folds:
            try:
                await slack_cli.post_message(
                    channel,
                    text=fold.text or fold.title,
                    blocks=fold.blocks,
                    thread_ts=ts,
                )
            except Exception as e:
                logger.warning("SlackTransport: thread 回复「%s」失败（主消息已发出，"
                               "不影响本次通知的成败）: %s", fold.title, e)
        return True

    async def send_text(self, target: NotifyTarget, text: str) -> bool:
        from app.services import slack_cli

        if not target.configured:
            logger.warning("SlackTransport.send_text: target 没配，跳过")
            return False
        channel = await self._resolve_channel(target)
        if not channel:
            return False
        try:
            return bool(await slack_cli.post_message(channel, text=text))
        except Exception as e:
            logger.error("SlackTransport.send_text 失败 channel=%s: %s", channel, e)
            return False
