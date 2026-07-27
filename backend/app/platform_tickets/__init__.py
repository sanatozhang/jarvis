"""
Platform Tickets — web/mcp/desktop 新平台工单隔离子模块

结构照搬 `app.crashguard`（独立表前缀 + 独立 models/migrations/config + 零跨界 FK），
但**不设 forbidden-import 合约**：本模块需要被 jarvis 核心统一读取层引用、
并接入共享 analysis_worker 分析流程。详见 `backend/app/platform_tickets/CLAUDE.md`。
"""

from app.platform_tickets.config import get_platform_tickets_settings  # noqa: F401

__all__ = ["get_platform_tickets_settings"]
