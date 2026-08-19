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
"""
from __future__ import annotations

from typing import Optional

from app.db import database as db

_KEY_PREFIX = "graygate_focus_version_"


def _key(platform: str) -> str:
    return f"{_KEY_PREFIX}{platform}"


async def set_focus_version(platform: str, version: str) -> None:
    await db.set_oncall_config(_key(platform), version)


async def clear_focus_version(platform: str) -> None:
    await db.set_oncall_config(_key(platform), "")


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
