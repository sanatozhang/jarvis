"""人工指定的"主要版本"——发布新版本时，运营通过 API（或前端 /settings 页面
的一个输入框）手动设置 iOS/Android 各自的精确 build 号，覆盖掉原先"session
数最大自动判定"的逻辑。

背景（2026-08-19）：原来的"🆕最新版本"层（自动取 build 号最大的包）被用户下线
——刚发布的包流量太薄，自动判定意义不大；而"主要版本"层原本也是纯自动判定
（session 数最大），但用户发布新版本后往往想立刻盯着这个新包，不想等它自然
爬到 session 数第一才被纳入报告。改成人工指定后，"主要版本"层默认还是自动
判定，一旦运营手动指定了某平台的版本，就改为跟踪这个指定值。

持久化：复用 `app.db.database` 现成的通用 (key, value) 配置表
`oncall_config`（跟复用 `CoreguardJobHeartbeat` 心跳表是同一个思路——临时/
轻量功能不新增表；这张表结构上就是纯 KV，历史上先给 oncall 用，不代表只能
存 oncall 相关 key，这里用 `graygate_focus_version_<platform>` 命名空间前缀
避免和其他 key 冲突）。

2026-09-15 新增审计 + 通知：用户实测发现 iOS 版本被设错却查不到是谁改的——
之前只是覆盖 KV 值，没有任何操作留痕，docker 容器日志还会随重启清空。现在
每次变更都：
1. 写一行 `GraygateFocusVersionAudit`（DB 表，不随进程/容器重启消失）；
2. 用"主" app（历史上 Apollo 的机器人身份，见 feishu_cli.py 顶部说明）推一条
   飞书通知到 graygate 配置的 `feishu_chat_id`（即"4.0灰度数据跟进群"）。
值没变化（例如重复点两次清空）不记录也不通知，避免噪声。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.db import database as db

logger = logging.getLogger("jarvis.graygate.focus_version")

_KEY_PREFIX = "graygate_focus_version_"


def _key(platform: str) -> str:
    return f"{_KEY_PREFIX}{platform}"


async def _notify_version_change(
    platform: str, old_value: str, new_value: str, changed_by: str,
) -> bool:
    """飞书通知版本变更，用"主" app 身份发送。发送失败不影响主流程（audit 仍会记录
    notify_sent=False，事后可查）。"""
    from app.graygate.config import get_graygate_settings
    from app.services.feishu_cli import send_interactive_card_as_main_app

    s = get_graygate_settings()
    if not s.feishu_enabled or not s.feishu_chat_id:
        return False

    action = "清空（回落 session 数自动判定）" if not new_value else f"设为 `{new_value}`"
    old_note = f"原值：`{old_value}`" if old_value else "原值：未设置（自动判定）"
    operator = changed_by or "未知（SSO 未登录）"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"🔖 4.0.3 灰度「主要版本」变更 · {platform.upper()}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{platform.upper()}** {action}"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": old_note}},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"操作人：{operator}"}},
        ],
    }
    try:
        return await send_interactive_card_as_main_app(chat_id=s.feishu_chat_id, card=card)
    except Exception:
        logger.exception("focus_version change notify failed (non-fatal)")
        return False


async def _record_and_notify(
    platform: str, *, old_value: str, new_value: str, changed_by: str,
) -> None:
    if old_value == new_value:
        return  # 值没变，不记录也不通知，避免噪声

    from app.db.database import get_session
    from app.graygate.models import GraygateFocusVersionAudit

    notify_sent = await _notify_version_change(platform, old_value, new_value, changed_by)

    async with get_session() as session:
        session.add(GraygateFocusVersionAudit(
            platform=platform, old_value=old_value, new_value=new_value,
            changed_by=changed_by, notify_sent=notify_sent,
        ))
        await session.commit()


async def set_focus_version(platform: str, version: str, changed_by: str = "") -> None:
    old = await get_focus_version(platform) or ""
    await db.set_oncall_config(_key(platform), version)
    await _record_and_notify(platform, old_value=old, new_value=version, changed_by=changed_by)


async def clear_focus_version(platform: str, changed_by: str = "") -> None:
    old = await get_focus_version(platform) or ""
    await db.set_oncall_config(_key(platform), "")
    await _record_and_notify(platform, old_value=old, new_value="", changed_by=changed_by)


async def get_focus_version(platform: str) -> Optional[str]:
    """返回人工指定的版本；未设置（或已被清空）时返回 None，调用方据此回落到
    session 数自动判定（`PlatformVersions.top_version`）。"""
    v = await db.get_oncall_config(_key(platform), "")
    return v or None


async def get_all_focus_versions() -> dict:
    return {
        "ios": await get_focus_version("ios"),
        "android": await get_focus_version("android"),
    }


async def get_focus_version_audit_history(platform: str = "", limit: int = 50) -> list:
    """最近 N 条变更审计记录，供 /focus-version/history 只读端点使用。"""
    from sqlalchemy import select
    from app.db.database import get_session
    from app.graygate.models import GraygateFocusVersionAudit

    q = select(GraygateFocusVersionAudit).order_by(GraygateFocusVersionAudit.changed_at.desc())
    if platform:
        q = q.where(GraygateFocusVersionAudit.platform == platform)
    q = q.limit(max(1, min(int(limit), 200)))

    async with get_session() as session:
        rows = (await session.execute(q)).scalars().all()

    return [
        {
            "platform": r.platform,
            "old_value": r.old_value,
            "new_value": r.new_value,
            "changed_by": r.changed_by,
            "changed_at": r.changed_at.isoformat() if r.changed_at else None,
            "notify_sent": r.notify_sent,
        }
        for r in rows
    ]
