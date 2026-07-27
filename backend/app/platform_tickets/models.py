"""
Platform Tickets SQLAlchemy 模型 — 单张 `pt_tickets` 表。

⚠️ 隔离子模块（结构照搬 `app.crashguard`）：
  - 表前缀固定 `pt_`，与 jarvis 老表（`issues`/`tasks`/...）和 crashguard
    表（`crash_*`）都不共享 schema。
  - **零跨界外键**：本模块任何表都不得有指向非 `pt_*` 表的外键（照 crashguard
    ADR-0001 同款约束，见 `backend/scripts/check_crash_decoupling.py`）。
  - 与 crashguard 的关键区别：**本模块不设 forbidden-import 合约**——
    web/mcp/desktop 工单要被 jarvis 核心统一读取层（`app.db.database`）
    引用、并走共享 `analysis_worker` 分析流程，这是同领域延伸而非独立领域，
    import 墙反而会挡住这种融入。详见 `backend/app/platform_tickets/CLAUDE.md`。

字段设计：只保留跨平台通用的生命周期字段（镜像 `IssueRecord` 的通用子集），
平台专属细节（web 的 url/browser、mcp 的 client/tool 等）一律塞进
`payload_json`，新增平台零迁移、零加列。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.db.database import Base


class PlatformTicket(Base):
    """新平台（web/mcp/desktop）工单存储 — `pt_tickets` 表。

    id 约定形如 `pt_<uuid hex>`（沿用现有 `fb_` 前缀风格），供
    `app.db.database.ticket_store_of()` 做 id→存储路由。
    """

    __tablename__ = "pt_tickets"

    id = Column(String(64), primary_key=True)
    platform = Column(String(16), default="")
    description = Column(Text, default="")
    priority = Column(String(4), default="")
    source = Column(String(16), default="")
    status = Column(String(32), default="pending", index=True)
    rule_type = Column(String(64), default="")
    category = Column(String(128), default="")
    created_by = Column(String(64), default="")
    occurred_at = Column(DateTime, nullable=True)
    deleted = Column(Boolean, default=False)

    # escalation（与 IssueRecord 的 escalation 全套字段对齐，供 oncall 统一层复用）
    escalated_at = Column(DateTime, nullable=True)
    escalated_by = Column(String(64), default="")
    escalation_note = Column(Text, default="")
    escalation_status = Column(String(16), default="")
    escalation_resolved_at = Column(DateTime, nullable=True)
    escalation_chat_id = Column(String(128), default="")
    escalation_share_link = Column(String(512), default="")
    escalation_reminded_at = Column(DateTime, nullable=True)

    created_at_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 平台专属字段（JSON）：web 的 url/browser/session、mcp 的 client/tool 等。
    # 新增平台不需要加列/迁移，直接扩展这个 JSON 载荷即可。
    payload_json = Column(Text, default="{}")
