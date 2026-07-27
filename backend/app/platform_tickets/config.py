"""
Platform Tickets 模块配置 — 极简占位（仿照 crashguard/coreguard 的 config.py 模式简化）。

加载顺序：env（`PT_*`）> 默认值。暂不需要 yaml 段 / 三层 kill switch，
后续如需持久化开关（如 UI 切换 enabled 免重启）再对齐 crashguard 的完整实现。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class PlatformTicketsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PT_", extra="ignore")

    enabled: bool = True


@lru_cache(maxsize=1)
def get_platform_tickets_settings() -> PlatformTicketsSettings:
    return PlatformTicketsSettings()
