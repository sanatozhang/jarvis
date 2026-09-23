"""FeishuTransport —— `IMTransport` 的飞书实现。

每个方法都是对 `services/feishu_cli.py` 现有函数的委托，**不重写逻辑**：
迁移期的目标是"飞书侧逐字节不变"，现有测试全绿就是这件事的证据。

飞书调用一律在方法体内部延迟 import —— 不要改成模块级
`from app.services.feishu_cli import X`，那样会让测试里
`patch("app.services.feishu_cli.X", ...)` 这种按模块属性 patch 的方式失效
（早绑定 vs 每次调用现查）。`tests/conftest.py` 的
`api_modules_with_local_get_settings` 列表记的就是这一类坑。
"""
from __future__ import annotations

import logging

from app.services.im.base import IMTransport, NotifyTarget, Rendered

logger = logging.getLogger("jarvis.services.im.feishu")


class FeishuTransport(IMTransport):
    provider = "feishu"

    async def send(self, target: NotifyTarget, msg: Rendered) -> bool:
        from app.services.feishu_cli import send_interactive_card

        if not target.configured:
            logger.warning("FeishuTransport.send: target 没配（channel 和 email 都空），跳过")
            return False
        # msg.folds 在飞书侧恒为空：折叠区是各模块的 builder 直接编译进
        # card 的 collapsible_panel，这里看不到也不需要看到。
        if msg.folds:
            logger.debug("FeishuTransport 忽略 %d 个 fold（飞书的折叠已在 card 内）",
                         len(msg.folds))
        if target.channel:
            return await send_interactive_card(chat_id=target.channel, card=msg.payload)
        return await send_interactive_card(email=target.email, card=msg.payload)

    async def send_text(self, target: NotifyTarget, text: str) -> bool:
        from app.services.feishu_cli import send_message

        if not target.configured:
            logger.warning("FeishuTransport.send_text: target 没配，跳过")
            return False
        if target.channel:
            return await send_message(chat_id=target.channel, text=text)
        return await send_message(email=target.email, text=text)
