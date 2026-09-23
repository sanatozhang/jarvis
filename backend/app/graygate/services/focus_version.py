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
2. 推一条变更通知到 graygate 配置的通知渠道（`services/notify.py`，跟灰度
   日报**同一个 target、同一个发送者身份**）。原本想用另一个"主" app 身份
   区分开，102 实测报 `230002 Bot/User can NOT be out of the chat`——那个
   app 根本不在"4.0灰度数据跟进群"里。这条约束在 Slack 侧同样成立（bot
   必须已经在那个私有频道里），所以不要在这里另配一个 target。
值没变化（例如重复点两次清空）不记录也不通知，避免噪声。

2026-09-16 新增鉴权：102 上实测发现有不明调用方持续覆盖这个值，写路径此前完全
不鉴权，任何人都能匿名调用。改成 API key 鉴权（见 `api/graygate.py::_resolve_caller`
和 `GraygateSettings.api_key_jarvis` / `api_key_runway`）——SSO 登录态用邮箱，
否则必须带合法 key，两者都没有直接 401，不再允许匿名调用；同时给每次真实变更
加了一行 INFO 日志，方便直接从容器日志里 grep 到"谁改的"。
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
    """通知「主要版本」变更，跟灰度日报走同一个渠道和同一个发送者身份。

    2026-09-15 实测（飞书路径）：一开始想用"主" app（历史上以为是 Apollo 的机器人
    身份）发送，结果 102 上实测报 `230002 Bot/User can NOT be out of the chat`
    ——那个 app 根本不在"4.0灰度数据跟进群"里。群里真正在用、灰度日报本身也在用
    的是 jarvis 自己的 IM 专属 app，所以这里必须复用日报的 target，不要另配。
    这条约束在 Slack 侧同样成立（bot 得在那个私有频道里），所以走
    `notify.report_target()` 而不是自己拼。

    发送失败不影响主流程（audit 仍会记录 notify_sent=False，事后可查）。
    """
    from app.graygate.config import get_graygate_settings
    from app.graygate.services import notify

    s = get_graygate_settings()
    if not s.send_enabled or not notify.report_target().configured:
        return False

    action = "清空（回落 session 数自动判定）" if not new_value else f"设为 `{new_value}`"
    old_note = f"原值：`{old_value}`" if old_value else "原值：未设置（自动判定）"
    operator = changed_by or "未知（调用方未提供身份）"
    try:
        return await notify.send_focus_change(platform, action, old_note, operator)
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

    # 2026-09-16：显式打一行 INFO 日志（不止 DB 审计）——102 上曾经排查"是谁在
    # 反复改这个值"时，只能从 uvicorn 访问日志里数 POST 次数、猜时间对不对得上，
    # 这行日志直接把 platform/旧值/新值/调用方一次性打全，`docker compose logs
    # backend | grep focus_version_changed` 就能定位，不用再翻访问日志对时间戳。
    logger.info(
        "focus_version_changed platform=%s %s -> %s changed_by=%s notify_sent=%s",
        platform, old_value or "(unset)", new_value or "(unset)", changed_by, notify_sent,
    )


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
