"""
Crashguard — 崩溃自动化分析与 PR 子模块

⚠️  这是独立模块，未来可能拆分为独立服务。
    模块隔离约束见 backend/app/crashguard/CLAUDE.md
"""

from app.crashguard.config import get_crashguard_settings  # noqa: F401

__all__ = ["get_crashguard_settings"]
