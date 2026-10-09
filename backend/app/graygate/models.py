"""Graygate SQLAlchemy 模型。

底层逻辑（2026-09-15 新增）：`services/focus_version.py` 的人工指定版本此前只是覆盖
一个 KV 值，没有任何操作留痕——用户实测发现 iOS 版本被设错却查不到是谁改的，
docker 容器日志还会随重启清空，连"何时调用过"都留不住。这张表专门兜底："不随
进程/容器重启消失"是硬性要求，必须落 DB，不能靠内存 dict 或 docker stdout。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Index, Integer, String, Text

from app.db.database import Base


class GraygateFocusVersionAudit(Base):
    """人工指定"主要版本"的变更审计：谁、什么时候、把哪个平台的版本从什么改成了什么。"""

    __tablename__ = "graygate_focus_version_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(16), nullable=False, index=True)  # ios / android
    old_value = Column(String(64), default="")
    new_value = Column(String(64), default="")
    # 登录用户 email（来自 request.state.user，SSO 未开启或未登录时为空）
    changed_by = Column(String(128), default="")
    changed_at = Column(DateTime, default=datetime.utcnow, index=True)
    notify_sent = Column(Boolean, default=False)  # 飞书通知是否发送成功

    __table_args__ = (
        Index("ix_graygate_focus_version_audit_platform_time", "platform", "changed_at"),
    )


class GraygateDailyReport(Base):
    """每日灰度日报的落库记录 + 网页端完整版 markdown 缓存（2026-10-09）。

    Slack 主消息只放核心指标，「查看完整日报 →」跳前端读这里的 `report_markdown`；
    现算要把 Datadog 再查一遍（几十个指标 × 两平台 × 三个时间窗），所以发送时就存下来。
    超过保留期的只清空 markdown、保留行（列表页要用），点开时在 Datadog 保留期内现算回填。
    """

    __tablename__ = "graygate_daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_date = Column(Date, nullable=False, unique=True, index=True)  # BJT 日历日（"昨日"）
    title = Column(String(128), default="")
    is_red = Column(Boolean, default=False)
    worsen_count = Column(Integer, default=0)
    new_crash_count = Column(Integer, default=0)
    report_markdown = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
