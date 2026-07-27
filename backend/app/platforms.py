"""
多平台工单支持 — 平台常量与归一化。

纯函数、零依赖模块：不 import 任何 app.* 子模块，可被任何地方安全引用
（app 核心 / platform_tickets 子模块 / analytics / 前端 API 层的后端映射均可用）。

平台维度贯穿：pt_tickets.platform / AnalysisRecord.platform / EventRecord.platform，
统一走 normalize_platform() 归一，避免大小写/空值在各处产生不一致的分组。
"""
from __future__ import annotations

from typing import Optional

# 支持的平台白名单，canonical 小写值。"app" 是历史默认平台（老 issues 表隐含平台）。
PLATFORMS = ["app", "web", "desktop", "mcp"]

_DEFAULT_PLATFORM = "app"


def normalize_platform(raw: Optional[str]) -> str:
    """归一化平台值。

    - None / "" / 纯空白 → "app"（历史默认平台，向后兼容 app 老工单无 platform 字段的场景）
    - 任意大小写的合法值（如 "APP" / "Web" / "MCP"）→ 对应 canonical 小写值
    - 未知值（不在 PLATFORMS 中）→ "app"（保守兜底，不让脏数据产生新的分组维度）
    """
    if raw is None:
        return _DEFAULT_PLATFORM
    value = raw.strip().lower()
    if not value:
        return _DEFAULT_PLATFORM
    if value not in PLATFORMS:
        return _DEFAULT_PLATFORM
    return value
