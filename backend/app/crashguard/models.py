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
    status = Column(String(32), default="open")  # open / investigating / resolved_by_pr / ignored / wontfix
    assignee = Column(String(64), default="")    # 指派人（jarvis 用户名）
    kind = Column(String(16), default="crash")   # crash / anr / memory / web_warning / other（见 categorizer）
    # C 路线：致命性分类——fatal（App 挂/卡：crash + ANR + App Hang）/ non_fatal（业务捕获异常）/ unknown
    fatality = Column(String(16), default="unknown", index=True)
    total_events = Column(Integer, default=0)
    total_users_affected = Column(Integer, default=0)
    representative_stack = Column(Text, default="")
    tags = Column(Text, default="{}")           # JSON
    external_refs = Column(Text, default="[]")  # JSON
    first_analyzed_at = Column(DateTime, nullable=True)  # 首次 AI 分析时间，去重用
    last_analyzed_at = Column(DateTime, nullable=True)   # 最近一次 AI 分析时间
    # Sprint 4 — RUM 分布缓存（每次 analyzer 运行时刷新）
    top_os = Column(String(256), default="")             # 例: "Android 14 (40%), Android 13 (20%)"
    top_device = Column(String(256), default="")         # 例: "Samsung SM-S911B (40%), Sony SO-52C (20%)"
    top_app_version = Column(String(128), default="")    # 例: "3.16.0-634 (60%), 3.15.1-631 (30%)"
    prewarm_attempts = Column(Integer, default=0)        # 已尝试预热次数
    prewarm_last_error = Column(Text, default="")        # 最近一次失败原因
    prewarm_last_at = Column(DateTime, nullable=True)    # 最近一次预热时间
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CrashSnapshot(Base):
    __tablename__ = "crash_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False)
    app_version = Column(String(32), default="")
    events_count = Column(Integer, default=0)
    users_affected = Column(Integer, default=0)         # Datadog 不直接返回，留待 Plan 2.5 RUM Events API
    sessions_affected = Column(Integer, default=0)      # Datadog impacted_sessions
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
    # Sprint 1.2 — 多根因 + 复杂度
    possible_causes = Column(Text, default="[]")     # JSON: [{title,evidence,confidence,code_pointer}]
    complexity_kind = Column(String(8), default="")  # simple / complex（区别于已有 complexity_level）
    solution = Column(Text, default="")              # simple 时：可执行 patch
    hint = Column(Text, default="")                  # complex 时：排查思路
    # Sprint 3 — 追问会话
    followup_question = Column(Text, default="")    # 用户的追问内容（首次分析为空）
    parent_run_id = Column(String(64), default="")  # 上一轮分析的 run_id；首次分析为空
    answer = Column(Text, default="")               # 追问轮次 AI 给的回答（独立于 root_cause）
    agent_model = Column(String(64), default="")    # 实际使用的模型（如 claude-sonnet-4-6[1m]）
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
    # GitHub PR 状态同步（pr_sync 服务回填）
    merged_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
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


class CrashHourlySnapshot(Base):
    """每小时事件数快照，用于 SHoW（Same Hour-of-Week）对比。

    底层逻辑：Plaud 用户横跨 JP/US/EU，hourly 流量天然有日内+周内双周期。
    用「上周同 weekday 同小时」做基线，10% 增长才是真信号。

    每个 (issue_id, hour_utc) 一条；upsert by unique 索引。
    hour_utc 用整点 UTC datetime，便于跨时区一致比较。
    """
    __tablename__ = "crash_hourly_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    datadog_issue_id = Column(String(128), nullable=False, index=True)
    hour_utc = Column(DateTime, nullable=False, index=True)  # 整点 UTC
    events_count = Column(Integer, default=0)
    captured_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "datadog_issue_id", "hour_utc",
            name="uq_crash_hourly_snapshots_issue_hour",
        ),
        Index("ix_crash_hourly_snapshots_hour", "hour_utc"),
    )


class CrashHourlyAlert(Base):
    """每小时告警发送幂等表，防多机重复触发。

    一条 = 一次告警发出；hour_utc 是发送时刻的整点。alert_payload 留 JSON 供回溯。
    """
    __tablename__ = "crash_hourly_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hour_utc = Column(DateTime, nullable=False)
    new_count = Column(Integer, default=0)
    surge_count = Column(Integer, default=0)
    feishu_message_id = Column(String(128), default="")
    alert_payload = Column(Text, default="{}")  # JSON
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("hour_utc", name="uq_crash_hourly_alerts_hour"),
    )


class CrashMetricSnapshot(Base):
    """10 分钟窗口的核心指标（crash-free sessions %）按平台快照。

    每个 (window_start, platform) 一条；upsert by unique 索引。
    用于：(1) 给当前 tick 比 rolling 1h baseline；(2) 长期 trend 回看。
    """
    __tablename__ = "crash_metric_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    window_start = Column(DateTime, nullable=False, index=True)  # 10 分钟窗口起点（UTC，floor 到 10min）
    platform = Column(String(16), nullable=False, index=True)    # android / ios / flutter / all
    total_sessions = Column(Integer, default=0)
    crashed_sessions = Column(Integer, default=0)
    crash_free_pct = Column(Float, default=100.0)                # (1 - crashed/total) * 100
    captured_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "window_start", "platform",
            name="uq_crash_metric_snapshots_window_platform",
        ),
        Index("ix_crash_metric_snapshots_window", "window_start"),
    )


class CrashMetricAlert(Base):
    """核心指标报警发送幂等表。

    一条 = 一次告警；window_start 是触发时的 10 分钟窗口起点。
    UNIQUE(window_start) 防多机重复发送（DB 抢锁兜底）。
    """
    __tablename__ = "crash_metric_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    window_start = Column(DateTime, nullable=False)
    platforms_alerted = Column(String(64), default="")           # 触发平台逗号串 "android,ios"
    direction = Column(String(8), default="")                    # up / down / mixed
    feishu_message_id = Column(String(128), default="")
    alert_payload = Column(Text, default="{}")                   # JSON
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("window_start", name="uq_crash_metric_alerts_window"),
    )


class CrashJobHeartbeat(Base):
    """定时任务心跳表 —— 每个 tick 写一条。

    底层逻辑：cron 类失败不会自动告警，靠人盯前端不可靠。心跳表是"运营级可观测性"的底座：
    - status / duration / error 三件套，前端表格 / 告警 / 复盘都从这一张表派生
    - last_success_at 单独索引，方便快速判定"X 任务多久没成功了"
    - JSON summary 存任务自报数据（如 alerted=true, items=5），无需 join 其它表
    """
    __tablename__ = "crash_job_heartbeats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_name = Column(String(32), nullable=False, index=True)     # core_metric / hourly_alert / morning_daily / ...
    fired_at = Column(DateTime, default=datetime.utcnow, index=True)
    status = Column(String(16), default="success")                # success / failed / skipped
    duration_ms = Column(Integer, default=0)
    summary = Column(Text, default="{}")                          # JSON: tick 自报的关键统计
    error = Column(Text, default="")

    __table_args__ = (
        Index("ix_crash_job_heartbeats_job_fired", "job_name", "fired_at"),
    )


class CrashAuditLog(Base):
    """运维 audit log：记录每次报告生成 / PR 创建 / 预热的成功失败结果。"""
    __tablename__ = "crash_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    op = Column(String(32), index=True, nullable=False)
    # daily_report / pr_draft / prewarm / batch_analyze / followup
    target_id = Column(String(128), default="")        # issue_id / analysis_id / "morning|evening"
    success = Column(Boolean, default=False)
    detail = Column(Text, default="")                  # JSON 或文本
    error = Column(Text, default="")
    duration_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
