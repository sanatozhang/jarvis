"""
Crashguard SQLAlchemy 模型 — 7 张 crash_* 表。

⚠️ 严禁外键指向非 crash_* 表，违反 ADR-0001。
"""
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)

from app.db.database import Base


class CrashIssue(Base):
    __tablename__ = "crash_issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), unique=True, nullable=False, index=True)
    stack_fingerprint = Column(String(64), index=True, default="")
    title = Column(String(512), default="")
    platform = Column(String(16), default="")  # flutter / ios / android
    service = Column(String(128), default="")
    first_seen_at = Column(DateTime, nullable=True)
    first_seen_version = Column(String(32), default="")
    last_seen_at = Column(DateTime, nullable=True)
    last_seen_version = Column(String(32), default="")
    status = Column(String(32), default="open")  # open / resolved_by_pr / ignored / wontfix
    total_events = Column(Integer, default=0)
    total_users_affected = Column(Integer, default=0)
    representative_stack = Column(Text, default="")
    tags = Column(Text, default="{}")           # JSON
    external_refs = Column(Text, default="[]")  # JSON
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CrashSnapshot(Base):
    __tablename__ = "crash_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False)
    app_version = Column(String(32), default="")
    events_count = Column(Integer, default=0)
    users_affected = Column(Integer, default=0)
    crash_free_rate = Column(Float, default=1.0)
    crash_free_impact_score = Column(Float, default=0.0)
    is_new_in_version = Column(Boolean, default=False)
    is_regression = Column(Boolean, default=False)
    is_surge = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "datadog_issue_id", "snapshot_date",
            name="uq_crash_snapshots_issue_date",
        ),
        Index(
            "ix_crash_snapshots_date_score",
            "snapshot_date", "crash_free_impact_score",
        ),
    )


class CrashFingerprint(Base):
    __tablename__ = "crash_fingerprints"

    fingerprint = Column(String(64), primary_key=True)
    datadog_issue_ids = Column(Text, default="[]")  # JSON 数组
    first_seen_version = Column(String(32), default="")
    total_events_across_versions = Column(Integer, default=0)
    normalized_top_frames = Column(Text, default="[]")  # JSON
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CrashAnalysis(Base):
    __tablename__ = "crash_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), index=True, nullable=False)
    analysis_run_id = Column(String(64), unique=True, nullable=False)
    agent_name = Column(String(32), default="")
    triggered_by = Column(String(32), default="scheduled")
    problem_type = Column(String(64), default="")
    root_cause = Column(Text, default="")
    scenario = Column(Text, default="")
    key_evidence = Column(Text, default="[]")  # JSON
    reproducibility = Column(String(32), default="unreproducible")
    verification_method = Column(String(16), default="static")  # static / unit_test
    verification_result = Column(String(32), default="")
    feasibility_score = Column(Float, default=0.0)
    feasibility_reasoning = Column(Text, default="")
    fix_suggestion = Column(Text, default="")
    fix_diff = Column(Text, nullable=True)
    reproduction_test_path = Column(String(256), nullable=True)
    reproduction_test_code = Column(Text, nullable=True)
    verification_log = Column(Text, default="")
    complexity_level = Column(String(8), default="high")  # low / high
    confidence = Column(String(8), default="low")
    agent_raw_output = Column(Text, default="")
    status = Column(String(16), default="success")  # success / failed
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CrashPullRequest(Base):
    __tablename__ = "crash_pull_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    analysis_id = Column(Integer, index=True, nullable=False)  # → crash_analyses.id (应用层 lookup)
    datadog_issue_id = Column(String(128), index=True, nullable=False)
    repo = Column(String(64), default="")  # plaud_ai / plaud_ios / plaud_android
    branch_name = Column(String(256), default="")
    pr_url = Column(String(512), default="")
    pr_number = Column(Integer, nullable=True)
    pr_status = Column(String(16), default="draft")  # draft / open / merged / closed
    triggered_by = Column(String(16), default="auto_verified")  # auto_verified / human_approved
    approved_by = Column(String(64), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    verification_status = Column(String(32), default="pending")
    verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CrashDailyReport(Base):
    __tablename__ = "crash_daily_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_date = Column(Date, nullable=False)
    report_type = Column(String(16), nullable=False)  # morning / evening
    top_n = Column(Integer, default=0)
    new_count = Column(Integer, default=0)
    regression_count = Column(Integer, default=0)
    surge_count = Column(Integer, default=0)
    feishu_message_id = Column(String(128), default="")
    report_payload = Column(Text, default="{}")  # JSON
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "report_date", "report_type",
            name="uq_crash_daily_reports_date_type",
        ),
    )


class CrashVersion(Base):
    __tablename__ = "crash_versions"

    version = Column(String(32), nullable=False)
    platform = Column(String(16), nullable=False)
    released_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=False)
    notes = Column(Text, default="")

    __table_args__ = (
        PrimaryKeyConstraint("version", "platform", name="pk_crash_versions"),
    )
