"""Coreguard SQLAlchemy 模型（demo 阶段：1 张表）。

正式版表清单见 design §4，这里 demo 只先建 snapshot。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
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
