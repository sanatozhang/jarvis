"""Coreguard SQLAlchemy 模型（demo 阶段：1 张表）。

正式版表清单见 design §4，这里 demo 只先建 snapshot。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.db.database import Base


class CoreguardMetricSnapshot(Base):
    __tablename__ = "coreguard_metric_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    metric_key = Column(String(64), nullable=False, index=True)
    window_start = Column(DateTime, nullable=False)   # UTC 整点
    value = Column(Float, nullable=True)              # 当前窗口值
    baseline_value = Column(Float, nullable=True)     # 上周同时段
    baseline_source = Column(String(32), default="show")  # show / rolling_fallback / none / error
    change = Column(Float, nullable=True)             # value - baseline（pp）或 (value-baseline)/baseline (pct)
    sessions_count = Column(Integer, nullable=True)
    breached = Column(Boolean, default=False)
    tier = Column(String(8), default="demo")          # demo 阶段固定 demo
    value_type = Column(String(16), default="percent_pp")
    alert_sent = Column(Boolean, default=False)
    extra = Column(Text, default="{}")                # JSON：debug 用
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("metric_key", "window_start", name="uq_coreguard_metric_window"),
        Index("ix_coreguard_metric_window_desc", "metric_key", "window_start"),
    )


class CoreguardAlertDispatch(Base):
    """coreguard 飞书告警下发记录。

    底层逻辑：群配额按 (sent_date_local, target_kind='group', sent_ok=True) 计数，
    超额转 overflow_email。dispatch 同时充当审计日志。
    """
    __tablename__ = "coreguard_alert_dispatches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 本地日期（Asia/Shanghai）— 配额按"自然日"算，跨夜 0 点清零
    sent_date = Column(Date, nullable=False, index=True)
    target_kind = Column(String(16), nullable=False)        # 'group' | 'email'
    target_value = Column(String(128), default="")          # chat_id 或邮箱
    sent_ok = Column(Boolean, default=False)
    alert_title = Column(String(256), default="")           # 卡片标题，审计用
    breach_count = Column(Integer, default=0)               # 本次告警 breach 指标数
    overflow_from_group = Column(Boolean, default=False)    # True=群配额满后转的个人
    sent_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_coreguard_alert_dispatch_date_kind", "sent_date", "target_kind"),
    )


class CoreguardJobHeartbeat(Base):
    """调度心跳（同 crashguard.crash_job_heartbeats 模式）。"""
    __tablename__ = "coreguard_job_heartbeats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_name = Column(String(64), nullable=False, index=True)
    fired_at = Column(DateTime, nullable=False, index=True)
    status = Column(String(16), default="ok")          # ok / failed / partial
    duration_ms = Column(Integer, default=0)
    summary = Column(Text, default="{}")               # JSON
    error = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_coreguard_heartbeat_job_fired", "job_name", "fired_at"),
    )
