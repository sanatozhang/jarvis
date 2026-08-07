"""
Database layer using SQLAlchemy async with SQLite/PostgreSQL.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Column, Date, DateTime, Integer, String, Text, Boolean, Float, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.platforms import normalize_platform


# ---------------------------------------------------------------------------
# ORM Base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
class IssueRecord(Base):
    __tablename__ = "issues"

    id = Column(String(64), primary_key=True)              # Feishu record_id or Linear issue ID
    description = Column(Text, default="")
    device_sn = Column(String(64), default="")
    firmware = Column(String(32), default="")
    app_version = Column(String(32), default="")
    priority = Column(String(4), default="")
    zendesk = Column(String(256), default="")
    zendesk_id = Column(String(32), default="")
    source = Column(String(16), default="feishu")          # feishu / linear / api / local
    feishu_link = Column(String(512), default="")
    linear_issue_id = Column(String(64), default="")       # e.g. "ENG-123"
    linear_issue_url = Column(String(512), default="")
    log_files_json = Column(Text, default="[]")            # JSON array
    status = Column(String(32), default="pending", index=True)  # pending / analyzing / done / failed / deleted
    rule_type = Column(String(64), default="")
    platform = Column(String(16), default="")              # APP / Web / Desktop
    category = Column(String(128), default="")             # problem category
    created_by = Column(String(64), default="")            # username who triggered analysis
    occurred_at = Column(DateTime, nullable=True)            # when the bug occurred (user-reported)
    deleted = Column(Boolean, default=False)
    escalated_at = Column(DateTime, nullable=True)
    escalated_by = Column(String(64), default="")
    escalation_note = Column(Text, default="")
    escalation_status = Column(String(16), default="")       # in_progress / resolved
    escalation_resolved_at = Column(DateTime, nullable=True)
    escalation_chat_id = Column(String(128), default="")    # Feishu group chat_id for sending resolve msg
    escalation_share_link = Column(String(512), default="") # Feishu group invite link for "join group" button
    escalation_reminded_at = Column(DateTime, nullable=True) # Last day-after reminder timestamp (avoid duplicate pings)
    created_at_ms = Column(Integer, default=0)             # creation time (Unix ms)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TaskRecord(Base):
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True)
    issue_id = Column(String(64), index=True)
    status = Column(String(32), default="queued")
    progress = Column(Integer, default=0)
    message = Column(Text, default="")
    agent_type = Column(String(32), default="")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AnalysisRecord(Base):
    __tablename__ = "analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), index=True)
    issue_id = Column(String(64), index=True)
    problem_type = Column(String(128), default="")
    problem_type_en = Column(String(128), default="")
    root_cause = Column(Text, default="")
    root_cause_en = Column(Text, default="")
    confidence = Column(String(16), default="medium")
    confidence_reason = Column(Text, default="")
    key_evidence_json = Column(Text, default="[]")
    user_reply = Column(Text, default="")
    user_reply_en = Column(Text, default="")
    needs_engineer = Column(Boolean, default=False)
    # T1 字段拆分：把"系统失败"和"需用户重传"从 needs_engineer 剥离
    # - system_failure: Agent 超时/额度耗尽/CLI 不可用 → ops 重跑就行，不必骚扰研发
    # - needs_user_retry: 日志解密失败/缺关键截图 → 客服找用户重传，不是研发问题
    system_failure = Column(Boolean, default=False)
    needs_user_retry = Column(Boolean, default=False)
    # T3 客服反馈闭环：让客服在工单详情页标记"AI 的工程师标签是否准确"
    # NULL=未反馈, True=确实需要工程师, False=AI 误判无需工程师
    engineer_label_feedback = Column(Boolean, nullable=True, default=None)
    engineer_label_feedback_by = Column(String(64), default="")
    engineer_label_feedback_at = Column(DateTime, nullable=True)
    engineer_label_feedback_note = Column(Text, default="")
    fix_suggestion = Column(Text, default="")
    problem_categories_json = Column(Text, default="[]")  # JSON: [{"category":"蓝牙连接","subcategory":"搜索不到设备"},...] — 冻结只读
    # VOC taxonomy 分类（新字段并存，与上面 problem_categories_json 独立）：
    # JSON: [{"tag_id","level_1_category","level_2_label","level_3_diagnosis",
    #         "role":"primary"|"secondary","confidence","reason"}, ...]
    voc_tags_json = Column(Text, default="[]")
    device_type = Column(String(64), default="")           # "Note" / "Note Pin" / "Note Pro" / "NotePin 2" / "iZYREC"
    rule_type = Column(String(64), default="")
    agent_type = Column(String(32), default="")
    agent_model = Column(String(128), default="")
    raw_output = Column(Text, default="")
    followup_question = Column(Text, default="")
    log_metadata_json = Column(Text, default="{}")  # JSON: extracted log metadata (uid, version, device, etc.)
    # 计量（2026-06-19）：每次分析/追问独立计费。total = agent + condenser；
    # total_tokens/total_cost_usd 供 analytics 按天 SUM，usage_json 存拆分明细供结果页展示。
    total_tokens = Column(Integer, default=0)        # (agent+condenser) input+output+cache 之和
    total_cost_usd = Column(Float, default=0.0)      # agent_cost + condenser_cost (USD)
    usage_json = Column(Text, default="{}")          # {"agent":{...,cost,source}, "condenser":{...,cost,model}}
    cost_source = Column(String(16), default="")     # cli_reported / computed / partial
    is_deep_analysis = Column(Boolean, default=False)  # 深度分析（全量日志）→ 结果页打 label
    # 多平台工单（阶段 2）：analytics 的平台维度靠给 analyses/events 打标实现，不 join 任何工单表。
    # 默认 "app"——每条 AnalysisRecord 必然对应一个具体工单，工单必然有平台（老 app 工单隐含平台即 app）。
    platform = Column(String(16), default="app")
    created_at = Column(DateTime, default=datetime.utcnow)


class VocTagRecord(Base):
    """VOC Portal taxonomy tag — runtime cache of https://voc-portal-apse1.nicebuild.click 的
    /api/taxonomy/tags。DB 是 runtime 真源（可被 UI/回填读），VOC 侧才是 taxonomy 真相源；
    模式仿 rule_engine.py（文件是 seed，DB 是 runtime 真源）。

    retired: VOC 的 /api/taxonomy/tags 只返回 active tags；本地把"存在于 DB 但这次同步
    没再收到"的 tag 标记为 retired（不再进新记录打标 prompt），已打标记录保留直到 re-tag。
    """
    __tablename__ = "voc_tags"

    id = Column(String(64), primary_key=True)              # "ai-01"
    level_1_category = Column(String(128), default="")     # group
    level_2_label = Column(String(128), default="")
    level_3_diagnosis = Column(String(128), default="")
    definition = Column(Text, default="")                  # 归类定义
    positive_examples_json = Column(Text, default="[]")    # ["...", ...]
    mece_rules_json = Column(Text, default="[]")            # [{"distinct_from":..,"reason":..}]
    negative_examples_json = Column(Text, default="[]")     # [{"example":..,"redirect_to":..}]
    updated_by = Column(String(128), default="")
    retired = Column(Boolean, default=False)
    synced_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VocWeeklyDigest(Base):
    """Cached weekly VOC insight digest (app.services.voc_digest). One row
    per ISO week (week_start = that week's Monday, "YYYY-MM-DD"). Generation
    involves an LLM call that can take tens of seconds, so this is a cache
    to read from on every page load, not a view recomputed per-request —
    regenerate via generate_weekly_digest(force=True), not by deleting rows."""
    __tablename__ = "voc_weekly_digests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    week_start = Column(String(10), unique=True, index=True, nullable=False)
    stats_json = Column(Text, default="{}")        # compute_weekly_stats() output
    narrative_json = Column(Text, default="null")  # LLM output dict, or JSON null if generation failed/disabled
    markdown = Column(Text, default="")
    model = Column(String(128), default="")
    total_tokens = Column(Integer, default=0)
    total_cost_usd = Column(Float, default=0.0)
    generated_at = Column(DateTime, default=datetime.utcnow)


class EventRecord(Base):
    """Core analytics events table."""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), index=True)      # analysis_start, analysis_done, analysis_fail, feedback_submit, page_visit, escalate
    issue_id = Column(String(64), default="")
    username = Column(String(64), default="")
    detail_json = Column(Text, default="{}")           # flexible payload
    duration_ms = Column(Integer, default=0)           # for timed events (analysis duration)
    # 多平台工单（阶段 2）：默认空字符串，不同于 AnalysisRecord 的 "app" 默认值——很多 EventRecord
    # 是通用埋点（无平台语境，如 page_visit），空串代表"未标注"，不能强行归一化成 app 掩盖这个事实。
    platform = Column(String(16), default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class RuleRecord(Base):
    __tablename__ = "rules"

    id = Column(String(64), primary_key=True)              # rule id e.g. "bluetooth"
    name = Column(String(128), default="")
    version = Column(Integer, default=1)
    enabled = Column(Boolean, default=True)
    triggers_json = Column(Text, default="{}")             # {"keywords":[], "priority":5}
    depends_on_json = Column(Text, default="[]")
    pre_extract_json = Column(Text, default="[]")
    needs_code = Column(Boolean, default=False)
    content = Column(Text, default="")                     # markdown body
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserRecord(Base):
    __tablename__ = "users"

    username = Column(String(64), primary_key=True)
    role = Column(String(16), default="user")              # admin / user
    feishu_email = Column(String(128), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, nullable=True)


class OncallGroupRecord(Base):
    __tablename__ = "oncall_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_index = Column(Integer, default=0)               # 0-based rotation order
    members_json = Column(Text, default="[]")              # ["email1@plaud.ai", "email2@plaud.ai"]
    created_by = Column(String(64), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OncallConfigRecord(Base):
    __tablename__ = "oncall_config"

    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")


class OncallWeekAssignmentRecord(Base):
    """排班快照(2026-07-24)：某一周实际值班归属的历史真相源。

    背景：`get_current_oncall()` 原来每次都用"当前组数"实时现算
    `weeks_elapsed % len(groups)`，新增/删除值班组会让分母瞬间改变，导致本周
    乃至全部历史周次的归属被一次编辑整体重新洗牌（102 实测：7 组时 14 周 %7=0
    对应正确的 chance/sanato.zhang，改成 8 组后 14%8=6 变成 jason.shao/victor）。

    这张表把"周 → 值班组"的映射从"现算"变成"写入即固定"：组配置变化时只重算
    当前周之后的未来周次（`_regenerate_week_assignments`，见 api/oncall.py），
    已经生成过的历史/当前周永远不会被后续编辑覆盖。

    `week_start_date` 沿用全代码库一致的"非自然周"定义：
    `start_date + timedelta(weeks=(某天-start_date).days//7)`，不对齐自然周一。

    `members_json` 存的是该周实际值班成员的邮箱快照（不是仅存 group_index）——
    `save_oncall_groups()` 是全删全建，组编辑后 group_index 对应的成员可能变化，
    只存索引会让"历史固定"变成假的固定；`group_index` 保留一列仅供展示参考，
    读取"谁值班"永远以 `members_json` 为准。
    """
    __tablename__ = "oncall_week_assignments"

    week_start_date = Column(Date, primary_key=True)
    week_end_date = Column(Date, nullable=False)
    group_index = Column(Integer, default=0)
    members_json = Column(Text, default="[]")
    generated_at = Column(DateTime, default=datetime.utcnow)


class GoldenSampleRecord(Base):
    __tablename__ = "golden_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_id = Column(String(64), index=True)
    analysis_id = Column(Integer)
    problem_type = Column(String(128), default="")
    description = Column(Text, default="")
    root_cause = Column(Text, default="")
    user_reply = Column(Text, default="")
    confidence = Column(String(16), default="high")
    rule_type = Column(String(64), default="")
    tags_json = Column(Text, default="[]")
    quality = Column(String(16), default="verified")
    created_by = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class EvalDatasetRecord(Base):
    __tablename__ = "eval_datasets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128))
    description = Column(Text, default="")
    sample_ids_json = Column(Text, default="[]")
    created_by = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class EvalRunRecord(Base):
    __tablename__ = "eval_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, index=True)
    status = Column(String(16), default="pending")
    config_json = Column(Text, default="{}")
    results_json = Column(Text, default="[]")
    summary_json = Column(Text, default="{}")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_by = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class WishRecord(Base):
    """Feature wishes / requests from users."""
    __tablename__ = "wishes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(256), default="")
    description = Column(Text, default="")
    status = Column(String(16), default="pending")  # pending / accepted / done / rejected
    votes = Column(Integer, default=0)
    created_by = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Release(Base):
    """One row per `release/X.Y.Z_MMDD` branch creation."""
    __tablename__ = "releases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    branch = Column(String(128), unique=True, nullable=False, index=True)
    version = Column(String(32), nullable=False, default="")   # "3.2.0"
    date_tag = Column(String(8), nullable=False, default="")   # "1222"
    repos_json = Column(Text, default="[]")                    # [{"name":"common","commit_sha":"..."}]
    created_by = Column(String(128), default="", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    status = Column(String(16), default="created")             # created / deleted


class ReleaseBuild(Base):
    """One row per Jenkins build trigger against a release branch."""
    __tablename__ = "release_builds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    branch = Column(String(128), nullable=False, index=True)
    target = Column(String(16), nullable=False, index=True)      # "cn" / "global"
    android_multi_channel = Column(Boolean, default=False)
    params_json = Column(Text, default="{}")                     # the exact form-data sent to Jenkins

    # Jenkins
    jenkins_server = Column(String(128), default="")
    jenkins_job = Column(String(128), default="")
    jenkins_queue_id = Column(Integer, nullable=True)
    jenkins_build_number = Column(Integer, nullable=True)
    jenkins_build_url = Column(String(512), default="")

    # state machine: pending / queued / running / success / failure / aborted / error
    status = Column(String(16), default="pending", index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, default="")

    # artifacts (filled by poller on success)
    artifact_android_url = Column(String(1024), default="")
    artifact_ios_url = Column(String(1024), default="")

    triggered_by = Column(String(128), default="", index=True)
    triggered_at = Column(DateTime, default=datetime.utcnow, index=True)


# ---------------------------------------------------------------------------
# Engine / Session
# ---------------------------------------------------------------------------
_engine = None
_session_factory = None


async def init_db():
    global _engine, _session_factory
    settings = get_settings()

    db_url = settings.database_url
    # For SQLite, resolve relative paths to absolute (relative to data_dir)
    if "sqlite" in db_url and ":///" in db_url:
        import re
        match = re.search(r"sqlite.*:///(.+)", db_url)
        if match:
            db_path = match.group(1)
            from pathlib import Path
            if not Path(db_path).is_absolute():
                abs_path = str(Path(settings.storage.data_dir) / Path(db_path).name)
                db_url = f"sqlite+aiosqlite:///{abs_path}"
            Path(abs_path if not Path(db_path).is_absolute() else db_path).parent.mkdir(parents=True, exist_ok=True)

    # SQLite needs WAL mode + busy_timeout to handle concurrent async writes.
    # 池策略：用 NullPool —— SQLite 单写者，连接复用反而加剧锁争用 + GC 泄漏。
    # NullPool 下每次请求开关一次 connection，开销 < 1ms，但彻底消灭了：
    #   1) "connection terminating" GC 泄漏；
    #   2) pool_timeout=30s 拿不到 slot 的雪崩；
    #   3) 一条 stale connection 持续污染整个池。
    connect_args = {}
    pool_kwargs = {}
    if "sqlite" in db_url:
        connect_args = {"timeout": 30}  # seconds to wait for lock
        from sqlalchemy.pool import NullPool
        pool_kwargs = {"poolclass": NullPool}

    _engine = create_async_engine(
        db_url,
        echo=False,
        connect_args=connect_args,
        **pool_kwargs,
    )

    # NullPool 下每次 connection 都是新建，必须用 connect event 给每条新 conn
    # 设上 PRAGMA。之前在 init_db 里一次性 set 只影响那一条，新 conn 全部
    # 默认 busy_timeout=0 → 拿不到锁就立刻抛 OperationalError。
    if "sqlite" in db_url:
        import sqlite3 as _sqlite3
        import time as _time
        from sqlalchemy import event

        # virtiofs (macOS Docker mount) 偶发 page-cache 错位 → SQLite 在新
        # connection 上执行 `PRAGMA journal_mode=WAL` 抛 "disk I/O error"。
        # DB 已经持久化在 WAL 模式（init_db 末尾验证过），所以这条 PRAGMA
        # 本质上是空操作 + 校验；transient I/O glitch 用退避重试即可。
        # 若 3 次仍失败，原样抛出 — 让下面的 handle_error 翻译成 disconnect，
        # 连接池丢弃坏 handle，下次 checkout 拿新 connection。
        # 上下文：2026-05-25 release_poller 30s tick 引爆，导致 ~14 min disk
        # I/O 错误风暴；本重试逻辑让单条 connection 对 virtiofs glitch 免疫。
        _IO_RETRY_BACKOFFS = (0.05, 0.10, 0.20)

        def _execute_pragma_with_retry(cursor, sql: str) -> None:
            last_exc = None
            for backoff in _IO_RETRY_BACKOFFS + (None,):
                try:
                    cursor.execute(sql)
                    return
                except _sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "disk i/o" not in msg and "database is locked" not in msg:
                        raise
                    last_exc = e
                    if backoff is None:
                        break
                    _time.sleep(backoff)
            assert last_exc is not None
            raise last_exc

        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas_on_connect(dbapi_conn, conn_record):
            cursor = dbapi_conn.cursor()
            try:
                _execute_pragma_with_retry(cursor, "PRAGMA journal_mode=WAL")
                _execute_pragma_with_retry(cursor, "PRAGMA busy_timeout=30000")
                # WAL 下用 NORMAL 同步级别（事务安全 + 大幅降低 fsync 阻塞）
                _execute_pragma_with_retry(cursor, "PRAGMA synchronous=NORMAL")
                # 每 1000 帧自动 checkpoint，避免 WAL 文件无限增长导致 reader 卡读
                _execute_pragma_with_retry(cursor, "PRAGMA wal_autocheckpoint=1000")
            finally:
                cursor.close()

        # 把 "disk I/O error" 翻译成 disconnect，让 SQLAlchemy 不要复用坏 handle
        @event.listens_for(_engine.sync_engine, "handle_error")
        def _treat_sqlite_io_as_disconnect(ctx):
            orig = ctx.original_exception
            if orig is None:
                return
            msg = str(orig).lower()
            if "disk i/o error" in msg or ("database is locked" in msg and "io" in msg):
                ctx.is_disconnect = True

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # 触发一次 connect 以应用 PRAGMA（顺便初始化 schema）
    if "sqlite" in db_url:
        async with _engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migrate: add new columns to existing tables (SQLite safe)
    async with _engine.begin() as conn:
        for col, coltype, default in [
            ("deleted", "BOOLEAN", "0"),
            ("created_by", "VARCHAR(64)", "''"),
            ("platform", "VARCHAR(16)", "''"),
            ("category", "VARCHAR(128)", "''"),
            ("source", "VARCHAR(16)", "'feishu'"),
            ("linear_issue_id", "VARCHAR(64)", "''"),
            ("linear_issue_url", "VARCHAR(512)", "''"),
            ("occurred_at", "DATETIME", "NULL"),
            ("escalated_at", "DATETIME", "NULL"),
            ("escalated_by", "VARCHAR(64)", "''"),
            ("escalation_note", "TEXT", "''"),
            ("escalation_status", "VARCHAR(16)", "''"),
            ("escalation_resolved_at", "DATETIME", "NULL"),
            ("escalation_chat_id", "VARCHAR(128)", "''"),
            ("escalation_share_link", "VARCHAR(512)", "''"),
            ("escalation_reminded_at", "DATETIME", "NULL"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE issues ADD COLUMN {col} {coltype} DEFAULT {default}"))
            except Exception:
                pass  # column already exists

        # Migrate analyses table
        for col, coltype, default in [
            ("problem_type_en", "VARCHAR(128)", "''"),
            ("root_cause_en", "TEXT", "''"),
            ("user_reply_en", "TEXT", "''"),
            ("followup_question", "TEXT", "''"),
            ("problem_categories_json", "TEXT", "'[]'"),
            ("device_type", "VARCHAR(64)", "''"),
            ("agent_model", "VARCHAR(128)", "''"),
            ("log_metadata_json", "TEXT", "'{}'"),
            ("system_failure", "BOOLEAN", "0"),       # T1: 系统失败标志（ops 重跑）
            ("needs_user_retry", "BOOLEAN", "0"),     # T1: 用户需重传日志/截图
            ("engineer_label_feedback", "BOOLEAN", "NULL"),       # T3: 客服反馈
            ("engineer_label_feedback_by", "VARCHAR(64)", "''"),  # T3
            ("engineer_label_feedback_at", "DATETIME", "NULL"),   # T3
            ("engineer_label_feedback_note", "TEXT", "''"),       # T3
            ("total_tokens", "INTEGER", "0"),          # 计量：token 总量
            ("total_cost_usd", "REAL", "0"),           # 计量：费用 USD
            ("usage_json", "TEXT", "'{}'"),            # 计量：拆分明细
            ("cost_source", "VARCHAR(16)", "''"),      # 计量：cli_reported/computed/partial
            ("is_deep_analysis", "BOOLEAN", "0"),      # 深度分析标记
            ("platform", "VARCHAR(16)", "'app'"),      # 多平台工单（阶段 2）：analytics 打标
            # VOC taxonomy 分类（新字段并存，problem_categories_json 冻结只读做对比）
            # JSON: [{"tag_id","level_1_category","level_2_label","level_3_diagnosis",
            #         "role":"primary"|"secondary","confidence","reason"}, ...]
            ("voc_tags_json", "TEXT", "'[]'"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE analyses ADD COLUMN {col} {coltype} DEFAULT {default}"))
            except Exception:
                pass

        # Migrate users table
        for col, coltype, default in [
            ("last_active_at", "DATETIME", "NULL"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {coltype} DEFAULT {default}"))
            except Exception:
                pass

        # Migrate events table (多平台工单阶段 2：platform 打标，默认空串="未标注"，与 analyses 的 "app" 语义不同)
        for col, coltype, default in [
            ("platform", "VARCHAR(16)", "''"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE events ADD COLUMN {col} {coltype} DEFAULT {default}"))
            except Exception:
                pass

        # Add indexes for frequently queried columns (safe to re-run)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_issues_status_updated ON issues(status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_issues_deleted ON issues(deleted)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_issue_id_created ON analyses(issue_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_issue_id_created ON tasks(issue_id, created_at DESC)",
        ]:
            try:
                await conn.execute(text(idx_sql))
            except Exception:
                pass


async def close_db():
    global _engine
    if _engine:
        await _engine.dispose()


def get_session() -> AsyncSession:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory()


# ---------------------------------------------------------------------------
# Retry helper —— SQLite WAL 下偶发 "database is locked" 在 busy_timeout 内
# 没等到锁就抛上来；读侧加 3 次指数退避重试，把 99% 的瞬时锁吸收掉。
# 写侧不包，让写失败快速暴露不掩盖真实问题。
# ---------------------------------------------------------------------------
import asyncio as _asyncio
import functools as _functools
import logging as _logging
from sqlalchemy.exc import OperationalError as _OperationalError

_retry_logger = _logging.getLogger("jarvis.db.retry")


def retry_on_lock(max_attempts: int = 3, base_delay: float = 0.05):
    """读侧装饰器：捕获 sqlite 'database is locked' 类异常，指数退避重试。

    只对读路径使用——写操作（upsert/update/delete）保持快失败。
    """
    def deco(fn):
        @_functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            attempt = 0
            delay = base_delay
            while True:
                try:
                    return await fn(*args, **kwargs)
                except _OperationalError as e:
                    msg = str(e).lower()
                    if "locked" not in msg and "busy" not in msg:
                        raise
                    attempt += 1
                    if attempt >= max_attempts:
                        _retry_logger.error(
                            "retry_on_lock exhausted for %s after %d attempts: %s",
                            fn.__name__, attempt, e,
                        )
                        raise
                    _retry_logger.warning(
                        "retry_on_lock %s attempt %d/%d after %.0fms: %s",
                        fn.__name__, attempt, max_attempts, delay * 1000, msg[:120],
                    )
                    await _asyncio.sleep(delay)
                    delay *= 2.5
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# Ticket store routing（阶段 3：id → 存储路由）
# ---------------------------------------------------------------------------
# 延迟到此处（而非文件顶部）import：`app.platform_tickets.models` 反向 `from
# app.db.database import Base`，顶部 import 会在 Base 类定义前触发循环导入。
# 此时 Base 已在本模块靠前位置定义完毕，从已加载到 sys.modules 的本模块对象上
# 取 Base 不会有问题。
from app.platform_tickets.models import PlatformTicket  # noqa: E402


def ticket_store_of(ticket_id: str) -> str:
    """按 id 前缀判断该工单存在哪个存储：'pt_' 前缀 → 'pt'，否则 → 'app'。"""
    return "pt" if (ticket_id or "").startswith("pt_") else "app"


async def get_ticket_record(session: AsyncSession, ticket_id: str):
    """路由查询：pt_ 前缀查 PlatformTicket，否则查 IssueRecord。返回 ORM 对象或 None。"""
    if ticket_store_of(ticket_id) == "pt":
        return await session.get(PlatformTicket, ticket_id)
    return await session.get(IssueRecord, ticket_id)


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------
async def upsert_issue(data: Dict[str, Any], status: str = "pending") -> IssueRecord:
    async with get_session() as session:
        rid = data.get("record_id") or data.get("id", "")
        existing = await session.get(IssueRecord, rid)
        if existing:
            existing.description = data.get("description", "") or existing.description
            existing.device_sn = data.get("device_sn", "") or existing.device_sn
            existing.firmware = data.get("firmware", "") or existing.firmware
            existing.app_version = data.get("app_version", "") or existing.app_version
            existing.priority = data.get("priority", "") or existing.priority
            existing.zendesk = data.get("zendesk", "") or existing.zendesk
            existing.zendesk_id = data.get("zendesk_id", "") or existing.zendesk_id
            existing.source = data.get("source", "") or existing.source
            existing.feishu_link = data.get("feishu_link", "") or existing.feishu_link
            existing.linear_issue_id = data.get("linear_issue_id", "") or existing.linear_issue_id
            existing.linear_issue_url = data.get("linear_issue_url", "") or existing.linear_issue_url
            existing.platform = data.get("platform", "") or existing.platform
            existing.category = data.get("category", "") or existing.category
            if data.get("created_at_ms"):
                existing.created_at_ms = data["created_at_ms"]
            if data.get("log_files"):
                existing.log_files_json = json.dumps(data["log_files"], ensure_ascii=False)
            if data.get("created_by"):
                existing.created_by = data["created_by"]
            if "occurred_at" in data:
                existing.occurred_at = data["occurred_at"]
            existing.status = status
            # 复位软删标记：重新导入/触发是「该工单重新生效」的明确信号，
            # 否则旧 deleted=True 残留 → 所有看板查询(deleted==False)永久隐藏该工单。
            # （2026-06-19 修：A 分析+导出飞书→B 删除→重新导入→看板找不到工单）
            existing.deleted = False
            existing.updated_at = datetime.utcnow()
            await session.commit()
            return existing
        record = IssueRecord(
            id=rid,
            description=data.get("description", ""),
            device_sn=data.get("device_sn", ""),
            firmware=data.get("firmware", ""),
            app_version=data.get("app_version", ""),
            priority=data.get("priority", ""),
            zendesk=data.get("zendesk", ""),
            zendesk_id=data.get("zendesk_id", ""),
            source=data.get("source", "feishu"),
            feishu_link=data.get("feishu_link", ""),
            linear_issue_id=data.get("linear_issue_id", ""),
            linear_issue_url=data.get("linear_issue_url", ""),
            platform=data.get("platform", ""),
            category=data.get("category", ""),
            created_by=data.get("created_by", ""),
            occurred_at=data.get("occurred_at"),
            created_at_ms=data.get("created_at_ms", 0),
            log_files_json=json.dumps(data.get("log_files", []), ensure_ascii=False),
            status=status,
            updated_at=datetime.utcnow(),
        )
        session.add(record)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.get(IssueRecord, rid)
            if existing:
                existing.status = status
                existing.deleted = False  # 同主分支：重新导入复位软删标记
                existing.updated_at = datetime.utcnow()
                await session.commit()
                return existing
            raise
        return record


async def update_issue_status(issue_id: str, status: str):
    async with get_session() as session:
        record = await get_ticket_record(session, issue_id)
        if record:
            record.status = status
            record.updated_at = datetime.utcnow()
            await session.commit()


async def escalate_issue(
    issue_id: str,
    escalated_by: str = "",
    note: str = "",
    chat_id: str = "",
    share_link: str = "",
) -> bool:
    async with get_session() as session:
        record = await get_ticket_record(session, issue_id)
        if not record:
            return False
        # Don't change status — keep the issue in its current tab (done/failed)
        # Only record escalation metadata so UI can show the badge
        record.escalated_at = datetime.utcnow()
        record.escalated_by = escalated_by
        record.escalation_note = note
        record.escalation_status = "in_progress"
        if chat_id:
            record.escalation_chat_id = chat_id
        if share_link:
            record.escalation_share_link = share_link
        record.escalation_reminded_at = None
        record.updated_at = datetime.utcnow()
        await session.commit()
        return True


async def update_escalation_share_link(issue_id: str, share_link: str) -> bool:
    """只刷新升级群分享链接（用于回填过期链接，不动其它 escalation 元数据）。"""
    if not share_link:
        return False
    async with get_session() as session:
        record = await get_ticket_record(session, issue_id)
        if not record:
            return False
        record.escalation_share_link = share_link
        record.updated_at = datetime.utcnow()
        await session.commit()
        return True


async def mark_escalation_reminded(issue_id: str) -> bool:
    async with get_session() as session:
        record = await get_ticket_record(session, issue_id)
        if not record:
            return False
        record.escalation_reminded_at = datetime.utcnow()
        record.updated_at = datetime.utcnow()
        await session.commit()
        return True


async def resolve_escalation(issue_id: str) -> bool:
    """Mark an escalated issue as resolved."""
    async with get_session() as session:
        record = await get_ticket_record(session, issue_id)
        if not record or not record.escalated_at:
            return False
        record.escalation_status = "resolved"
        record.escalation_resolved_at = datetime.utcnow()
        record.updated_at = datetime.utcnow()
        await session.commit()
        return True


async def get_escalated_issues(status: str | None = None, since_date=None) -> List[Dict[str, Any]]:
    """Get escalated issues, optionally filtered by status and date cutoff.

    统一读取层（阶段 3）：`issues`（IssueRecord）+ `pt_tickets`（PlatformTicket）
    各自按同一组筛选条件查询 → Python 侧合并 → 按 `escalated_at` desc 重新排序。
    `pt_tickets` 目前为空，故对现有 app 流量输出逐字节相同。
    """
    from sqlalchemy import select as sa_select

    def _build_stmt(model):
        stmt = sa_select(model).where(
            model.escalated_at.isnot(None),
            model.deleted == False,
        )
        if status:
            stmt = stmt.where(model.escalation_status == status)
        if since_date:
            cutoff = datetime(since_date.year, since_date.month, since_date.day)
            stmt = stmt.where(model.escalated_at >= cutoff)
        return stmt

    async with get_session() as session:
        app_result = await session.execute(_build_stmt(IssueRecord))
        pt_result = await session.execute(_build_stmt(PlatformTicket))
        issues = list(app_result.scalars().all()) + list(pt_result.scalars().all())
        issues.sort(key=lambda i: i.escalated_at or datetime.min, reverse=True)

        items = []
        for issue in issues:
            # Inline latest analysis query — AnalysisRecord 只靠 issue_id 字符串
            # 关联，不区分来源存储，两表工单都吃得到，保持不变。
            a_stmt = sa_select(AnalysisRecord).where(
                AnalysisRecord.issue_id == issue.id
            ).order_by(AnalysisRecord.created_at.desc()).limit(1)
            a_result = await session.execute(a_stmt)
            analysis = a_result.scalar_one_or_none()

            items.append({
                "record_id": issue.id,
                "description": issue.description or "",
                "problem_type": analysis.problem_type if analysis else "",
                "problem_type_en": getattr(analysis, "problem_type_en", "") or "" if analysis else "",
                "root_cause": analysis.root_cause if analysis else "",
                "confidence": analysis.confidence if analysis else "",
                "user_reply": analysis.user_reply if analysis else "",
                # zendesk_id 是 app 专属列，PlatformTicket 没有该属性 → 容错取空
                "zendesk_id": getattr(issue, "zendesk_id", "") or "",
                "source": issue.source or "",
                "escalated_at": (issue.escalated_at.isoformat() + "Z") if issue.escalated_at else "",
                "escalated_by": issue.escalated_by or "",
                "escalation_note": issue.escalation_note or "",
                "escalation_status": issue.escalation_status or "in_progress",
                "escalation_resolved_at": (issue.escalation_resolved_at.isoformat() + "Z") if issue.escalation_resolved_at else "",
                "escalation_chat_id": issue.escalation_chat_id or "",
                "escalation_share_link": issue.escalation_share_link or "",
                "created_at": (issue.created_at.isoformat() + "Z") if issue.created_at else "",
            })
        return items


async def soft_delete_issue(issue_id: str) -> bool:
    async with get_session() as session:
        record = await get_ticket_record(session, issue_id)
        if record:
            record.deleted = True
            record.updated_at = datetime.utcnow()
            await session.commit()
            return True
        return False


async def set_issue_created_by(issue_id: str, username: str):
    async with get_session() as session:
        record = await get_ticket_record(session, issue_id)
        if record and username:
            record.created_by = username
            await session.commit()


async def get_recent_active_task_for_issue(
    issue_id: str,
    within_minutes: int = 10,
) -> Optional[TaskRecord]:
    """Return the most recent non-failed task for issue_id created within the last N minutes.

    Used to throttle duplicate analysis triggers (e.g. webhook double-fire,
    impatient re-analyze clicks). Returns None if no such task exists.
    """
    from datetime import timedelta
    from sqlalchemy import select
    cutoff = datetime.utcnow() - timedelta(minutes=within_minutes)
    async with get_session() as session:
        stmt = (
            select(TaskRecord)
            .where(
                TaskRecord.issue_id == issue_id,
                TaskRecord.status.in_(["queued", "analyzing", "done"]),
                TaskRecord.created_at >= cutoff,
            )
            .order_by(TaskRecord.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_recent_timeout_task_for_issue(
    issue_id: str,
    within_minutes: int = 10,
) -> Optional[TaskRecord]:
    """Return the most recent timeout-failed task for issue_id in last N minutes.

    Reason: 同一条工单（同 prompt 同日志）刚因 task_timeout_exceeded 失败，立刻
    重跑是确定性会再次超时的浪费 —— 反而把孤儿子进程问题放大。这里把它当成
    专用 dedup 信号：UI 上立刻再点「重试」会被拒绝，要等冷却期。
    """
    from datetime import timedelta
    from sqlalchemy import select
    cutoff = datetime.utcnow() - timedelta(minutes=within_minutes)
    async with get_session() as session:
        stmt = (
            select(TaskRecord)
            .where(
                TaskRecord.issue_id == issue_id,
                TaskRecord.status == "failed",
                TaskRecord.error.like("task_timeout_exceeded%"),
                TaskRecord.created_at >= cutoff,
            )
            .order_by(TaskRecord.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def create_task(task_id: str, issue_id: str, agent_type: str = "") -> TaskRecord:
    async with get_session() as session:
        record = TaskRecord(
            id=task_id,
            issue_id=issue_id,
            agent_type=agent_type,
            status="queued",
        )
        session.add(record)
        await session.commit()
        return record


async def update_task(
    task_id: str,
    status: Optional[str] = None,
    progress: Optional[int] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
):
    async with get_session() as session:
        record = await session.get(TaskRecord, task_id)
        if record is None:
            return
        if status is not None:
            record.status = status
        if progress is not None:
            record.progress = progress
        if message is not None:
            record.message = message
        if error is not None:
            record.error = error
        record.updated_at = datetime.utcnow()
        await session.commit()


async def get_task(task_id: str) -> Optional[TaskRecord]:
    async with get_session() as session:
        return await session.get(TaskRecord, task_id)


async def get_latest_done_task_for_issue(issue_id: str) -> Optional[TaskRecord]:
    """Get the most recent successful task for an issue (for follow-up workspace reuse)."""
    async with get_session() as session:
        from sqlalchemy import select
        stmt = (
            select(TaskRecord)
            .where(TaskRecord.issue_id == issue_id, TaskRecord.status == "done")
            .order_by(TaskRecord.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_prior_followup_history(
    issue_id: str, exclude_task_id: str = ""
) -> Tuple[int, List[str]]:
    """该 issue 的历史分析链 → (prior_analysis_count, prior_followup_questions[oldest→newest])。

    ⚠️ followup_question 列在 analyses 表（AnalysisRecord），不在 tasks 表（TaskRecord）——
    必须查 AnalysisRecord，否则 `t.followup_question` 会抛 AttributeError 致追问失败。

    - prior_analysis_count：已存在的历史分析数（不含当前 task）。追问递进放宽窗口的「深度」。
    - prior_followup_questions：历史追问文本（去空，时间正序）。用于重裁锚点 + prompt 历史。
    """
    async with get_session() as session:
        from sqlalchemy import select
        stmt = (
            select(AnalysisRecord)
            .where(AnalysisRecord.issue_id == issue_id)
            .order_by(AnalysisRecord.created_at.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        prior = [a for a in rows if a.task_id != exclude_task_id]
        questions = [q for q in ((a.followup_question or "").strip() for a in prior) if q]
        return len(prior), questions


def normalize_device_type(raw: str) -> str:
    """Normalize device type to canonical form.

    Strips 'Plaud' prefix, normalizes casing and spacing.
    Canonical types: Note, Note Pro, Note Pin, NotePin 2
    """
    if not raw:
        return ""
    # Strip parenthetical info like "(SN: xxx)" and leading "Plaud"
    s = re.sub(r"\s*\(.*?\)\s*", "", raw).strip()
    s = re.sub(r"(?i)^plaud\s*", "", s).strip()
    if not s:
        return ""
    low = s.lower().replace(" ", "")
    if low in ("notepro", "notpro"):
        return "Note Pro"
    if low in ("notepin2", "notpin2", "notepin2nd"):
        return "NotePin 2"
    if low in ("notepin", "notpin"):
        return "Note Pin"
    if low in ("note",):
        return "Note"
    if low in ("izyrec",):
        return "iZYREC"
    # Fallback: title-case the cleaned string
    return s


async def save_analysis(data: Dict[str, Any]) -> AnalysisRecord:
    # problem_categories / classify_problem() keyword classification retired
    # 2026-08 in favor of the VOC Portal taxonomy (voc_tags below) — the
    # agent no longer outputs problem_categories (see app.agents.base), and
    # this backend-side fallback is intentionally NOT re-enabled for new
    # rows: doing so would keep repopulating a field this feature explicitly
    # freezes. classify_problem()/classification_taxonomy.py stay in the
    # repo (used only by the historical /api/analytics/backfill-classifications
    # endpoint, now also guarded — see get_analyses_for_backfill below) so
    # old pre-cutover data stays comparable.
    categories = data.get("problem_categories", [])

    # VOC taxonomy (new classification, stored alongside problem_categories above,
    # which stays frozen). Unlike problem_categories, there is deliberately NO
    # backend keyword fallback here — an LLM call inside the hot save_analysis
    # path would add network latency/failure modes to every ticket write.
    # Rows the AI left empty are picked up later by the backfill script's
    # only_empty scan (see app.services.voc_classifier / scripts/backfill_voc_tags.py).
    voc_tags = data.get("voc_tags", []) or []

    # 多平台工单（阶段 2）：platform 优先取顶层 "platform" key（未来 pt_tickets 流程 / 测试可直传），
    # 否则退化到 AnalysisResult 里 denormalized 的 issue.platform（tasks.py/queue.py 的 result.issue 主路径）。
    # 老式调用（既无顶层 platform 也无 issue）→ normalize_platform("") == "app"，向后兼容零改动。
    raw_platform = data.get("platform")
    if not raw_platform:
        issue_payload = data.get("issue") or {}
        if isinstance(issue_payload, dict):
            raw_platform = issue_payload.get("platform", "")
        else:
            raw_platform = getattr(issue_payload, "platform", "") or ""

    async with get_session() as session:
        record = AnalysisRecord(
            task_id=data.get("task_id", ""),
            issue_id=data.get("issue_id", ""),
            platform=normalize_platform(raw_platform),
            problem_type=data.get("problem_type", ""),
            problem_type_en=data.get("problem_type_en", ""),
            problem_categories_json=json.dumps(categories, ensure_ascii=False),
            voc_tags_json=json.dumps(voc_tags, ensure_ascii=False),
            device_type=normalize_device_type(data.get("device_type", "")),
            root_cause=data.get("root_cause", ""),
            root_cause_en=data.get("root_cause_en", ""),
            confidence=data.get("confidence", "medium"),
            confidence_reason=data.get("confidence_reason", ""),
            key_evidence_json=json.dumps(data.get("key_evidence", []), ensure_ascii=False),
            user_reply=data.get("user_reply", ""),
            user_reply_en=data.get("user_reply_en", ""),
            needs_engineer=data.get("needs_engineer", False),
            system_failure=data.get("system_failure", False),
            needs_user_retry=data.get("needs_user_retry", False),
            fix_suggestion=data.get("fix_suggestion", ""),
            rule_type=data.get("rule_type", ""),
            agent_type=data.get("agent_type", ""),
            agent_model=data.get("agent_model", ""),
            raw_output=data.get("raw_output", ""),
            followup_question=data.get("followup_question", ""),
            log_metadata_json=json.dumps(data.get("log_metadata", {}), ensure_ascii=False),
            total_tokens=int(data.get("total_tokens", 0) or 0),
            total_cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
            usage_json=json.dumps(data.get("usage_breakdown", {}), ensure_ascii=False),
            cost_source=data.get("cost_source", ""),
            is_deep_analysis=bool(data.get("is_deep_analysis", False)),
        )
        session.add(record)
        await session.commit()
        return record


async def get_analysis_by_issue(issue_id: str) -> Optional[AnalysisRecord]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(AnalysisRecord).where(
            AnalysisRecord.issue_id == issue_id
        ).order_by(AnalysisRecord.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


@retry_on_lock()
async def get_all_analyses_by_issue(issue_id: str) -> List[AnalysisRecord]:
    """Get ALL analyses for an issue, ordered by created_at DESC (newest first)."""
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(AnalysisRecord).where(
            AnalysisRecord.issue_id == issue_id
        ).order_by(AnalysisRecord.created_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_analysis_by_task(task_id: str) -> Optional[AnalysisRecord]:
    """Get a single analysis by task_id."""
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(AnalysisRecord).where(
            AnalysisRecord.task_id == task_id
        ).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_analyses_by_date(date_str: str) -> List[AnalysisRecord]:
    """Get all analyses for a given date (YYYY-MM-DD)."""
    async with get_session() as session:
        from sqlalchemy import select, cast, Date
        stmt = select(AnalysisRecord).where(
            func.date(AnalysisRecord.created_at) == date_str
        ).order_by(AnalysisRecord.created_at)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_tasks(limit: int = 50) -> List[TaskRecord]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Local issue queries (for 进行中 / 已完成 tabs)
# ---------------------------------------------------------------------------
@retry_on_lock()
async def get_local_issue_ids() -> set:
    """
    Get issue IDs that should be EXCLUDED from the pending list.
    Excludes analyzing (进行中) and done/failed (已完成).
    """
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(IssueRecord.id).where(
            IssueRecord.status.in_(["analyzing", "failed", "done", "inaccurate"]),
            IssueRecord.deleted == False,
        )
        result = await session.execute(stmt)
        return {row[0] for row in result.fetchall()}


@retry_on_lock()
async def get_local_issues_paginated(
    status: str,
    page: int = 1,
    page_size: int = 20,
) -> tuple:
    """
    Get issues by local status with pagination.
    status can be a single value or comma-separated: "analyzing,failed"
    Returns (items: List[Dict], total: int).
    """
    async with get_session() as session:
        from sqlalchemy import select, func

        statuses = [s.strip() for s in status.split(",")]
        status_filter = IssueRecord.status.in_(statuses) & (IssueRecord.deleted == False)

        # Count total
        count_stmt = select(func.count()).select_from(IssueRecord).where(status_filter)
        total = (await session.execute(count_stmt)).scalar() or 0

        # Get page
        offset = (page - 1) * page_size
        stmt = select(IssueRecord).where(
            status_filter
        ).order_by(IssueRecord.updated_at.desc()).offset(offset).limit(page_size)
        issues = list((await session.execute(stmt)).scalars().all())

        # Batch-load analyses, tasks, and counts for all issues in one go
        items = await _enrich_issues_batch(session, issues)
        return items, total


@retry_on_lock()
async def get_tracked_issues_paginated(
    page: int = 1,
    page_size: int = 20,
    created_by: Optional[str] = None,
    platform: Optional[str] = None,
    category: Optional[str] = None,
    status_filter: Optional[str] = None,
    source: Optional[str] = None,
    zendesk_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple:
    """
    Get ALL locally-tracked issues (for the tracking page).
    Supports multiple filters. Excludes deleted.

    统一读取层（阶段 3）：跨 `issues`（IssueRecord）+ `pt_tickets`（PlatformTicket）
    合并。`zendesk_id` 是 app 专属字段（pt 表没有这一列），只对 IssueRecord 生
    效——指定该筛选时 pt 侧直接不参与查询（不是忽略条件误把所有 pt 工单纳入）。
    其余通用字段（created_by/platform/category/status_filter/source/date
    range）两表各自套用同一组值各自过滤。

    排序/分页语义与改前完全一致：**先把两表匹配结果合并成全量列表按
    updated_at desc 排序，再 offset/limit 切片**，不是"各自分页再拼接"（否则
    跨表边界页会错）。`total` = 两表 count 之和。`pt_tickets` 目前为空，这一步
    对现有 app 数据的输出与改前逐字节相同。
    """
    async with get_session() as session:
        from sqlalchemy import select, func, and_

        def _conditions(model, include_zendesk: bool):
            conds = [model.deleted == False, model.status != "pending"]
            if created_by:
                conds.append(model.created_by == created_by)
            if platform:
                conds.append(model.platform == platform)
            if category:
                conds.append(model.category.contains(category))
            if status_filter:
                conds.append(model.status == status_filter)
            if source:
                conds.append(model.source == source)
            if include_zendesk and zendesk_id:
                conds.append(model.zendesk_id.contains(zendesk_id.strip("#")))
            if date_from:
                conds.append(model.created_at >= datetime.fromisoformat(date_from))
            if date_to:
                conds.append(model.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
            return and_(*conds)

        app_where = _conditions(IssueRecord, include_zendesk=True)

        app_count_stmt = select(func.count()).select_from(IssueRecord).where(app_where)
        app_total = (await session.execute(app_count_stmt)).scalar() or 0
        app_stmt = select(IssueRecord).where(app_where).order_by(IssueRecord.updated_at.desc())
        app_issues = list((await session.execute(app_stmt)).scalars().all())

        if zendesk_id:
            # pt 工单没有 zendesk_id 这个概念：指定该筛选时 pt 侧不返回任何结果。
            pt_total = 0
            pt_issues: List[Any] = []
        else:
            pt_where = _conditions(PlatformTicket, include_zendesk=False)
            pt_count_stmt = select(func.count()).select_from(PlatformTicket).where(pt_where)
            pt_total = (await session.execute(pt_count_stmt)).scalar() or 0
            pt_stmt = select(PlatformTicket).where(pt_where).order_by(PlatformTicket.updated_at.desc())
            pt_issues = list((await session.execute(pt_stmt)).scalars().all())

        total = app_total + pt_total

        merged = app_issues + pt_issues
        merged.sort(key=lambda i: i.updated_at or datetime.min, reverse=True)

        offset = (page - 1) * page_size
        page_issues = merged[offset:offset + page_size]

        items = await _enrich_issues_batch(session, page_issues)
        return items, total


async def _enrich_issues_batch(
    session: AsyncSession,
    issues: "List[IssueRecord | PlatformTicket]",
) -> List[Dict[str, Any]]:
    """Batch-load analysis + task data for a list of issues.

    Uses 2 batch queries (analysis + count) + per-issue task lookup
    within the same session to avoid N+1 session overhead.

    `issues` 阶段 3 起可以混合 IssueRecord（app）和 PlatformTicket（新平台）——
    这里只用 `issue.id` 关联 AnalysisRecord/TaskRecord，两种类型都通用，无需特判。
    """
    from sqlalchemy import select, func

    if not issues:
        return []

    issue_ids = [issue.id for issue in issues]

    # 1. Latest analysis per issue — AnalysisRecord.id is auto-increment Integer
    latest_a_sub = (
        select(func.max(AnalysisRecord.id).label("max_id"))
        .where(AnalysisRecord.issue_id.in_(issue_ids))
        .group_by(AnalysisRecord.issue_id)
    ).subquery()
    a_stmt = select(AnalysisRecord).where(AnalysisRecord.id.in_(select(latest_a_sub.c.max_id)))
    analyses = {a.issue_id: a for a in (await session.execute(a_stmt)).scalars().all()}

    # 2. Analysis count per issue
    count_stmt = (
        select(AnalysisRecord.issue_id, func.count().label("cnt"))
        .where(AnalysisRecord.issue_id.in_(issue_ids))
        .group_by(AnalysisRecord.issue_id)
    )
    a_counts = dict((await session.execute(count_stmt)).all())

    # 3. All tasks for these issues, then pick latest per issue in Python
    #    (TaskRecord.id is a String UUID — can't use max(id) for ordering)
    all_tasks_stmt = (
        select(TaskRecord)
        .where(TaskRecord.issue_id.in_(issue_ids))
        .order_by(TaskRecord.created_at.desc())
    )
    all_tasks = (await session.execute(all_tasks_stmt)).scalars().all()
    tasks: Dict[str, TaskRecord] = {}
    for t in all_tasks:
        if t.issue_id not in tasks:  # first one is latest (ordered by created_at desc)
            tasks[t.issue_id] = t

    return [
        _issue_to_dict(
            issue,
            analysis=analyses.get(issue.id),
            task=tasks.get(issue.id),
            analysis_count=a_counts.get(issue.id, 0),
        )
        for issue in issues
    ]


def _issue_to_dict(
    issue: "IssueRecord | PlatformTicket",
    analysis: Optional[AnalysisRecord] = None,
    task: Optional[TaskRecord] = None,
    analysis_count: int = 0,
) -> Dict[str, Any]:
    """Convert DB records to a dict matching the frontend Issue+Result shape.

    `issue` 阶段 3 起可以是 IssueRecord（app 老表）或 PlatformTicket（新平台，
    `pt_tickets` 表）。PlatformTicket 没有 device_sn/firmware/app_version/
    feishu_link/linear_issue_id/linear_issue_url/log_files_json/zendesk/
    zendesk_id 这些 app 专属列——一律用 `getattr(issue, name, default)` 容错读
    取，不能假设该属性存在；缺失字段在返回 dict 里给空字符串/空列表。
    """
    log_files_json = getattr(issue, "log_files_json", None)
    payload_json = getattr(issue, "payload_json", None)
    d: Dict[str, Any] = {
        "record_id": issue.id,
        "description": issue.description or "",
        "device_sn": getattr(issue, "device_sn", "") or "",
        "firmware": getattr(issue, "firmware", "") or "",
        "app_version": getattr(issue, "app_version", "") or "",
        "priority": issue.priority or "",
        "zendesk": getattr(issue, "zendesk", "") or "",
        "zendesk_id": getattr(issue, "zendesk_id", "") or "",
        "source": issue.source or "feishu",
        "feishu_link": getattr(issue, "feishu_link", "") or "",
        "feishu_status": issue.status or "pending",
        "linear_issue_id": getattr(issue, "linear_issue_id", "") or "",
        "linear_issue_url": getattr(issue, "linear_issue_url", "") or "",
        "result_summary": "",
        "root_cause_summary": "",
        "created_at_ms": issue.created_at_ms or 0,
        "log_files": json.loads(log_files_json) if log_files_json else [],
        "local_status": issue.status,
        "platform": issue.platform or "",
        "category": issue.category or "",
        "created_by": issue.created_by or "",
        "created_at": (issue.created_at.isoformat() + "Z") if issue.created_at else "",
        "occurred_at": (issue.occurred_at.isoformat() + "Z") if issue.occurred_at else "",
        "analysis_count": analysis_count,
        "escalated_at": (issue.escalated_at.isoformat() + "Z") if issue.escalated_at else "",
        "escalated_by": issue.escalated_by or "",
        "escalation_note": issue.escalation_note or "",
        "escalation_status": issue.escalation_status or "",
        "escalation_resolved_at": (issue.escalation_resolved_at.isoformat() + "Z") if issue.escalation_resolved_at else "",
        "escalation_chat_id": issue.escalation_chat_id or "",
        "escalation_share_link": issue.escalation_share_link or "",
        # 多平台工单（阶段 3）：pt 工单的平台专属字段（web 的 url/browser/session、
        # mcp 的 client/tool 等）从 payload_json 解出，塞进这个新键。IssueRecord
        # 没有 payload_json 属性 → 给 {}。这是本阶段唯一新增的输出字段，其余现
        # 有字段的值/结构必须与改前完全一致。
        "platform_meta": json.loads(payload_json) if payload_json else {},
    }

    if analysis:
        d["analysis"] = {
            "id": analysis.id,
            "task_id": analysis.task_id,
            "issue_id": analysis.issue_id,
            "problem_type": analysis.problem_type or "",
            "problem_type_en": analysis.problem_type_en or "",
            "root_cause": analysis.root_cause or "",
            "root_cause_en": analysis.root_cause_en or "",
            "confidence": analysis.confidence or "medium",
            "confidence_reason": analysis.confidence_reason or "",
            "key_evidence": json.loads(analysis.key_evidence_json) if analysis.key_evidence_json else [],
            "user_reply": analysis.user_reply or "",
            "user_reply_en": analysis.user_reply_en or "",
            "needs_engineer": analysis.needs_engineer,
            "system_failure": getattr(analysis, "system_failure", False) or False,
            "needs_user_retry": getattr(analysis, "needs_user_retry", False) or False,
            # T3: 客服反馈状态
            "engineer_label_feedback": getattr(analysis, "engineer_label_feedback", None),
            "engineer_label_feedback_by": getattr(analysis, "engineer_label_feedback_by", "") or "",
            "engineer_label_feedback_at": (
                (analysis.engineer_label_feedback_at.isoformat() + "Z")
                if getattr(analysis, "engineer_label_feedback_at", None) else ""
            ),
            "engineer_label_feedback_note": getattr(analysis, "engineer_label_feedback_note", "") or "",
            "fix_suggestion": analysis.fix_suggestion or "",
            "rule_type": analysis.rule_type or "",
            "agent_type": analysis.agent_type or "",
            "agent_model": getattr(analysis, "agent_model", "") or "",
            "followup_question": analysis.followup_question or "",
            "log_metadata": json.loads(analysis.log_metadata_json) if getattr(analysis, "log_metadata_json", None) else {},
            "created_at": (analysis.created_at.isoformat() + "Z") if analysis.created_at else "",
            "total_tokens": int(getattr(analysis, "total_tokens", 0) or 0),
            "total_cost_usd": float(getattr(analysis, "total_cost_usd", 0.0) or 0.0),
            "usage_breakdown": json.loads(analysis.usage_json) if getattr(analysis, "usage_json", None) else {},
            "cost_source": getattr(analysis, "cost_source", "") or "",
            "is_deep_analysis": bool(getattr(analysis, "is_deep_analysis", False)),
        }
        d["result_summary"] = analysis.user_reply or ""
        d["result_summary_en"] = analysis.user_reply_en or ""
        d["root_cause_summary"] = analysis.root_cause or ""
        d["root_cause_summary_en"] = analysis.root_cause_en or ""

    if task:
        d["task"] = {
            "task_id": task.id,
            "status": task.status,
            "progress": task.progress,
            "message": task.message or "",
            "error": task.error,
        }

    return d


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------
ADMIN_USERNAME = "sanato"  # initial admin


def _norm_username(username: str) -> str:
    """Canonical username key: strip + lowercase.

    Both login paths store usernames lowercase (users.py does `.strip().lower()`;
    auth.py derives via derive_username_from_email which lowercases). Read/write
    helpers must apply the same normalization so a display-case username
    (e.g. "WM" from the escalate request) still resolves the stored "wm" row —
    otherwise the creator's email never resolves and they get dropped from the
    escalation group.
    """
    return (username or "").strip().lower()


async def upsert_user(
    username: str,
    feishu_email: str = "",
    role: Optional[str] = None,
) -> Dict[str, Any]:
    username = _norm_username(username)
    async with get_session() as session:
        resolved_role = role if role else (
            "admin" if username == ADMIN_USERNAME else "user"
        )
        record = UserRecord(
            username=username,
            role=resolved_role,
            feishu_email=feishu_email,
        )
        merged = await session.merge(record)
        await session.commit()
        return {
            "username": merged.username,
            "role": merged.role,
            "feishu_email": merged.feishu_email,
        }


async def update_user_feishu_email(username: str, feishu_email: str) -> Optional[Dict[str, Any]]:
    """Update only the feishu_email field for an existing user. Returns None if user not found.

    Used by the bind-callback flow where we want to attach an email to a legacy
    username without disturbing role or creating a new row.
    """
    username = _norm_username(username)
    async with get_session() as session:
        record = await session.get(UserRecord, username)
        if not record:
            return None
        record.feishu_email = feishu_email
        await session.commit()
        return {
            "username": record.username,
            "role": record.role,
            "feishu_email": record.feishu_email,
        }


async def get_user(username: str) -> Optional[Dict[str, Any]]:
    username = _norm_username(username)
    async with get_session() as session:
        from sqlalchemy import select
        record = await session.get(UserRecord, username)
        if not record:
            return None
        return {"username": record.username, "role": record.role, "feishu_email": record.feishu_email}


async def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Look up a user by feishu_email, regardless of which flow (legacy local
    registration or a prior Feishu login) created the row. `feishu_email` has
    no uniqueness constraint, so if duplicates exist we deterministically pick
    the oldest account (created_at asc) so account identity doesn't shift
    between logins.
    """
    email = (email or "").lower().strip()
    if not email:
        return None
    async with get_session() as session:
        from sqlalchemy import select
        stmt = (
            select(UserRecord)
            .where(UserRecord.feishu_email == email)
            .order_by(UserRecord.created_at.asc())
        )
        result = await session.execute(stmt)
        record = result.scalars().first()
        if not record:
            return None
        return {"username": record.username, "role": record.role, "feishu_email": record.feishu_email}


async def get_or_create_user(username: str) -> Dict[str, Any]:
    user = await get_user(username)
    if user:
        return user
    return await upsert_user(username)


async def touch_user_active(username: str):
    username = _norm_username(username)
    if not username:
        return
    async with get_session() as session:
        user = await session.get(UserRecord, username)
        if user:
            user.last_active_at = datetime.utcnow()
            await session.commit()


async def list_users() -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select, func
        stmt = select(UserRecord).order_by(UserRecord.created_at)
        result = await session.execute(stmt)
        users = result.scalars().all()

        user_list = []
        for u in users:
            count_stmt = select(func.count()).select_from(EventRecord).where(EventRecord.username == u.username)
            count_result = await session.execute(count_stmt)
            action_count = count_result.scalar() or 0

            user_list.append({
                "username": u.username,
                "role": u.role,
                "feishu_email": u.feishu_email or "",
                "created_at": (u.created_at.isoformat() + "Z") if u.created_at else "",
                "last_active_at": (u.last_active_at.isoformat() + "Z") if u.last_active_at else "",
                "action_count": action_count,
            })
        return user_list


# ---------------------------------------------------------------------------
# Oncall CRUD
# ---------------------------------------------------------------------------
async def save_oncall_groups(groups: List[List[str]], created_by: str = ""):
    """Replace all oncall groups with new ones."""
    async with get_session() as session:
        from sqlalchemy import delete
        await session.execute(delete(OncallGroupRecord))
        for idx, members in enumerate(groups):
            session.add(OncallGroupRecord(
                group_index=idx,
                members_json=json.dumps(members, ensure_ascii=False),
                created_by=created_by,
            ))
        await session.commit()


async def get_oncall_groups() -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(OncallGroupRecord).order_by(OncallGroupRecord.group_index)
        result = await session.execute(stmt)
        return [
            {"group_index": r.group_index, "members": json.loads(r.members_json) if r.members_json else []}
            for r in result.scalars().all()
        ]


async def set_oncall_config(key: str, value: str):
    async with get_session() as session:
        record = OncallConfigRecord(key=key, value=value)
        await session.merge(record)
        await session.commit()


async def get_oncall_config(key: str, default: str = "") -> str:
    async with get_session() as session:
        record = await session.get(OncallConfigRecord, key)
        return record.value if record else default


async def _find_latest_week_anchor(before: date) -> Optional[Dict[str, Any]]:
    """找 `before` 之前最近一次已冻结/生成的排班快照，用作续轮锚点。

    2026-07-27 修复：组数变化后，「冻结本周」之后的所有周次原先一律按
    `绝对周数 % 新组数` 现算——这会在冻结周和下一周之间造成轮转跳跃（实测：
    8 组时本周冻结为 group 0，下一周本该接着轮到 group 1，却因为
    `week_num % 8` 直接跳到 group 7）。查不到返回 None（纯历史空洞——本功能
    上线前的周次，或从未被任何快照覆盖过——调用方应回退绝对取模兜底）。
    """
    from sqlalchemy import select as sa_select
    async with get_session() as session:
        stmt = (
            sa_select(OncallWeekAssignmentRecord)
            .where(OncallWeekAssignmentRecord.week_start_date < before)
            .order_by(OncallWeekAssignmentRecord.week_start_date.desc())
            .limit(1)
        )
        record = (await session.execute(stmt)).scalar_one_or_none()
        if record is None:
            return None
        return {"week_start": record.week_start_date, "group_index": record.group_index}


async def get_week_assignment(week_start: date) -> Optional[Dict[str, Any]]:
    """查排班快照表某一周,查不到返回 None(调用方应回退现算)。"""
    async with get_session() as session:
        record = await session.get(OncallWeekAssignmentRecord, week_start)
        if record is None:
            return None
        return {
            "week_start": record.week_start_date,
            "week_end": record.week_end_date,
            "group_index": record.group_index,
            "members": json.loads(record.members_json) if record.members_json else [],
        }


async def upsert_week_assignment(
    week_start: date, week_end: date, group_index: int, members: List[str],
    *, only_if_missing: bool = False,
) -> None:
    """写入/覆盖某一周的排班快照。

    `only_if_missing=True` 时,已有行就跳过不覆盖——用于"冻结当前周"这个语义:
    组配置变化时,若本周从没被冻结过才写入一次,已经冻结过的本周不会因为同一周内
    再次编辑而被重新计算。
    """
    async with get_session() as session:
        existing = await session.get(OncallWeekAssignmentRecord, week_start)
        if existing is not None:
            if only_if_missing:
                return
            existing.week_end_date = week_end
            existing.group_index = group_index
            existing.members_json = json.dumps(members, ensure_ascii=False)
            existing.generated_at = datetime.utcnow()
        else:
            session.add(OncallWeekAssignmentRecord(
                week_start_date=week_start, week_end_date=week_end,
                group_index=group_index, members_json=json.dumps(members, ensure_ascii=False),
            ))
        await session.commit()


async def resolve_week_group(week_num: int, groups: List[Dict[str, Any]], start: date) -> Dict[str, Any]:
    """给定 week_num,返回该周实际值班组 {group_index, members, week_start, week_end}。

    优先查排班快照表(历史/当前周一旦生成即固定,不受后续组数变化影响);查不到
    时**优先续轮**：找最近一次已冻结的快照锚点，从其 group_index 往后顺延
    （2026-07-27 修复：组数变化后紧邻的下一周不再对绝对周数取模跳变，而是接着
    冻结周继续轮转）；连锚点都找不到（本次功能上线之前的纯历史空洞）才现算
    `week_num % len(groups)` 兜底。这是全代码库"周→组"计算的唯一权威入口，
    `get_current_oncall`/`/stats`/`resolve_duty_week` 均应改为调用此函数，
    不再各自重复实现取模公式。
    """
    week_start = start + timedelta(weeks=week_num)
    week_end = week_start + timedelta(days=6)
    snap = await get_week_assignment(week_start)
    if snap is not None:
        return {
            "group_index": snap["group_index"], "members": snap["members"],
            "week_start": week_start, "week_end": week_end,
        }
    n = len(groups) if groups else 0
    if n == 0:
        idx = 0
    else:
        anchor = await _find_latest_week_anchor(week_start)
        if anchor is not None:
            weeks_since_anchor = (week_start - anchor["week_start"]).days // 7
            idx = (anchor["group_index"] + weeks_since_anchor) % n
        else:
            idx = week_num % n  # 纯历史空洞（本功能上线前），无锚点可续，退回绝对取模兜底
    return {
        "group_index": idx, "members": groups[idx]["members"] if groups else [],
        "week_start": week_start, "week_end": week_end,
    }


async def get_current_oncall_info() -> Dict[str, Any]:
    """本周值班组完整信息(members + group_index),供 `/current` 接口用。"""
    groups = await get_oncall_groups()
    if not groups:
        return {"members": [], "group_index": -1}
    start_date_str = await get_oncall_config("start_date", "")
    if not start_date_str:
        return {"members": groups[0]["members"], "group_index": 0}
    try:
        start = date.fromisoformat(start_date_str)
        today = date.today()
        week_num = max(0, (today - start).days // 7)
        info = await resolve_week_group(week_num, groups, start)
        return {"members": info["members"], "group_index": info["group_index"]}
    except Exception:
        return {"members": groups[0]["members"], "group_index": 0}


async def get_current_oncall() -> List[str]:
    """Get the current week's oncall members based on rotation(优先查排班快照)。"""
    info = await get_current_oncall_info()
    return info["members"]


# ---------------------------------------------------------------------------
# Rule DB CRUD
# ---------------------------------------------------------------------------
async def upsert_rule_to_db(rule_data: Dict[str, Any]):
    """Save a rule to the database."""
    async with get_session() as session:
        record = RuleRecord(
            id=rule_data["id"],
            name=rule_data.get("name", ""),
            version=rule_data.get("version", 1),
            enabled=rule_data.get("enabled", True),
            triggers_json=json.dumps(rule_data.get("triggers", {}), ensure_ascii=False),
            depends_on_json=json.dumps(rule_data.get("depends_on", []), ensure_ascii=False),
            pre_extract_json=json.dumps(rule_data.get("pre_extract", []), ensure_ascii=False),
            needs_code=rule_data.get("needs_code", False),
            content=rule_data.get("content", ""),
        )
        await session.merge(record)
        await session.commit()


async def get_all_rules_from_db() -> List[Dict[str, Any]]:
    """Get all rules from the database."""
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(RuleRecord).order_by(RuleRecord.name)
        result = await session.execute(stmt)
        rules = []
        for r in result.scalars().all():
            rules.append({
                "id": r.id,
                "name": r.name,
                "version": r.version,
                "enabled": r.enabled,
                "triggers": json.loads(r.triggers_json) if r.triggers_json else {},
                "depends_on": json.loads(r.depends_on_json) if r.depends_on_json else [],
                "pre_extract": json.loads(r.pre_extract_json) if r.pre_extract_json else [],
                "needs_code": r.needs_code,
                "content": r.content,
            })
        return rules


async def delete_rule_from_db(rule_id: str) -> bool:
    async with get_session() as session:
        record = await session.get(RuleRecord, rule_id)
        if record:
            await session.delete(record)
            await session.commit()
            return True
        return False


# ---------------------------------------------------------------------------
# Event tracking (analytics)
# ---------------------------------------------------------------------------
async def log_event(
    event_type: str,
    issue_id: str = "",
    username: str = "",
    detail: Optional[Dict] = None,
    duration_ms: int = 0,
    platform: str = "",
):
    """Log an analytics event.

    多平台工单（阶段 2）：platform 为可选打标。留空字符串代表"未标注"（很多 EventRecord
    是通用埋点，没有平台语境），不像 AnalysisRecord 那样强行兜底成 "app"——空串不能被
    normalize_platform() 静默改写成 "app"，否则会掩盖"这条事件没打标"这个事实。
    """
    async with get_session() as session:
        session.add(EventRecord(
            event_type=event_type,
            issue_id=issue_id,
            username=username,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
            duration_ms=duration_ms,
            platform=normalize_platform(platform) if platform else "",
        ))
        await session.commit()
    await touch_user_active(username)


async def get_analytics(date_from: str, date_to: str) -> Dict[str, Any]:
    """Get analytics summary for a date range."""
    async with get_session() as session:
        from sqlalchemy import select, func, and_, case

        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to + "T23:59:59")
        date_filter = and_(EventRecord.created_at >= start, EventRecord.created_at <= end)

        # Total events by type
        type_counts_stmt = select(
            EventRecord.event_type, func.count()
        ).where(date_filter).group_by(EventRecord.event_type)
        type_counts = {row[0]: row[1] for row in (await session.execute(type_counts_stmt)).fetchall()}

        # Unique users
        users_stmt = select(func.count(func.distinct(EventRecord.username))).where(
            date_filter, EventRecord.username != ""
        )
        unique_users = (await session.execute(users_stmt)).scalar() or 0

        # Average analysis duration (for done events)
        avg_duration_stmt = select(func.avg(EventRecord.duration_ms)).where(
            date_filter, EventRecord.event_type == "analysis_done", EventRecord.duration_ms > 0
        )
        avg_duration = (await session.execute(avg_duration_stmt)).scalar() or 0

        # Fail reasons (with issue_id, username, duration, timestamp for drill-down)
        fail_stmt = select(
            EventRecord.issue_id,
            EventRecord.detail_json,
            EventRecord.username,
            EventRecord.duration_ms,
            EventRecord.created_at,
        ).where(
            date_filter, EventRecord.event_type == "analysis_fail"
        ).order_by(EventRecord.created_at.desc()).limit(100)
        fail_details = []
        for row in (await session.execute(fail_stmt)).fetchall():
            try:
                detail = json.loads(row.detail_json) if row.detail_json else {}
            except Exception:
                detail = {}
            fail_details.append({
                "issue_id": row.issue_id or "",
                "username": row.username or "",
                "duration_ms": row.duration_ms or 0,
                "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
                **detail,
            })

        # Daily breakdown
        daily_stmt = select(
            func.date(EventRecord.created_at).label("day"),
            EventRecord.event_type,
            func.count(),
        ).where(date_filter).group_by("day", EventRecord.event_type).order_by("day")
        daily_rows = (await session.execute(daily_stmt)).fetchall()
        daily = {}
        for day, etype, count in daily_rows:
            d = str(day)
            if d not in daily:
                daily[d] = {}
            daily[d][etype] = count

        # 计量：按天聚合 analyses 的 token / 费用（含追问，每条独立计），合并进 daily
        cost_stmt = select(
            func.date(AnalysisRecord.created_at).label("day"),
            func.sum(AnalysisRecord.total_tokens),
            func.sum(AnalysisRecord.total_cost_usd),
        ).where(
            func.date(AnalysisRecord.created_at) >= date_from,
            func.date(AnalysisRecord.created_at) <= date_to,
        ).group_by("day").order_by("day")
        period_tokens = 0
        period_cost = 0.0
        for day, tok, cost in (await session.execute(cost_stmt)).fetchall():
            d = str(day)
            t = int(tok or 0)
            c = float(cost or 0.0)
            daily.setdefault(d, {})
            daily[d]["tokens"] = t
            daily[d]["cost_usd"] = round(c, 4)
            period_tokens += t
            period_cost += c

        # Top users (only meaningful actions, exclude page_visit)
        _meaningful_events = ("analysis_start", "analysis_done", "analysis_fail", "feedback_submit", "escalate")
        top_users_stmt = select(
            EventRecord.username, func.count()
        ).where(date_filter, EventRecord.username != "", EventRecord.event_type.in_(_meaningful_events)).group_by(
            EventRecord.username
        ).order_by(func.count().desc()).limit(10)
        top_users = [{"username": row[0], "count": row[1]} for row in (await session.execute(top_users_stmt)).fetchall()]

        # Separate external failures (token quota, disk space, etc.) from real service failures.
        # External failures should not count against the success rate.
        _EXTERNAL_FAIL_REASONS = {"OpenAI 额度不足", "Claude 额度不足", "所有模型额度不足", "磁盘空间不足", "token 额度不足", "API 额度不足"}
        external_fail_count = 0
        for fd in fail_details:
            reason = fd.get("reason", "")
            if reason in _EXTERNAL_FAIL_REASONS:
                external_fail_count += 1

        total_fail = type_counts.get("analysis_fail", 0)
        real_fail = total_fail - external_fail_count

        # 追问（follow-up）拆分子项：
        # - 成功追问：以 analyses 表为准（followup_question 自 2026-03-02 起逐条落库，历史完整）
        # - 失败追问：失败 task 不落 analyses，只能查 events 的 analysis_fail.detail_json
        #   （followup_question 自 2026-06-19 commit e080eda 起才写入，更早的失败追问无标记）
        followup_done_stmt = select(func.count()).select_from(AnalysisRecord).where(
            func.date(AnalysisRecord.created_at) >= date_from,
            func.date(AnalysisRecord.created_at) <= date_to,
            AnalysisRecord.followup_question != "",
        )
        followup_done = (await session.execute(followup_done_stmt)).scalar() or 0

        ff_stmt = select(EventRecord.detail_json).where(
            date_filter, EventRecord.event_type == "analysis_fail"
        )
        followup_fail = 0
        for (dj,) in (await session.execute(ff_stmt)).fetchall():
            try:
                if dj and (json.loads(dj).get("followup_question") or "").strip():
                    followup_fail += 1
            except Exception:
                pass

        return {
            "date_from": date_from,
            "date_to": date_to,
            "event_counts": type_counts,
            "unique_users": unique_users,
            "avg_analysis_duration_ms": round(avg_duration),
            "avg_analysis_duration_min": round(avg_duration / 60000, 1) if avg_duration else 0,
            "fail_reasons": fail_details,
            "daily": daily,
            "top_users": top_users,
            "total_analyses": type_counts.get("analysis_start", 0),
            "successful_analyses": type_counts.get("analysis_done", 0),
            "failed_analyses": real_fail,
            "followup_done": followup_done,
            "followup_fail": followup_fail,
            "external_failures": external_fail_count,
            "feedback_submitted": type_counts.get("feedback_submit", 0),
            "escalations": type_counts.get("escalate", 0),
            # 计量：本期总 token / 费用（analytics Daily Trend 顶部汇总卡 + 折线右轴）
            "total_tokens": period_tokens,
            "total_cost_usd": round(period_cost, 4),
        }


# ---------------------------------------------------------------------------
# Problem Type Statistics
# ---------------------------------------------------------------------------
async def get_problem_type_stats(date_from: str, date_to: str) -> Dict[str, Any]:
    """Get problem type distribution, trend, and top 10 for a date range."""
    async with get_session() as session:
        from sqlalchemy import select, func, and_

        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to + "T23:59:59")
        # Exclude agent meta-commentary that leaked into problem_type
        _INVALID_TYPES = ["", "未知", "Analysis Complete", "分析完成", "分析总结",
                          "Unknown", "问题定位完成", "分析结果", "Completed", "Done", "N/A"]
        date_filter = and_(
            AnalysisRecord.created_at >= start,
            AnalysisRecord.created_at <= end,
            AnalysisRecord.problem_type.notin_(_INVALID_TYPES),
        )

        # 1) Count per problem_type
        dist_stmt = select(
            AnalysisRecord.problem_type,
            AnalysisRecord.problem_type_en,
            func.count().label("count"),
        ).where(date_filter).group_by(
            AnalysisRecord.problem_type, AnalysisRecord.problem_type_en,
        ).order_by(func.count().desc())
        dist_rows = (await session.execute(dist_stmt)).fetchall()

        distribution = [
            {"problem_type": r.problem_type, "problem_type_en": r.problem_type_en or r.problem_type, "count": r.count}
            for r in dist_rows
        ]
        total = sum(d["count"] for d in distribution)

        # 2) Daily trend for top 10 categories
        top_types = [d["problem_type"] for d in distribution[:10]]
        trend: Dict[str, Dict[str, int]] = {}
        if top_types:
            trend_stmt = select(
                func.date(AnalysisRecord.created_at).label("day"),
                AnalysisRecord.problem_type,
                func.count().label("count"),
            ).where(
                and_(date_filter, AnalysisRecord.problem_type.in_(top_types))
            ).group_by("day", AnalysisRecord.problem_type).order_by("day")
            for row in (await session.execute(trend_stmt)).fetchall():
                d = str(row.day)
                if d not in trend:
                    trend[d] = {}
                trend[d][row.problem_type] = row.count

        return {
            "date_from": date_from,
            "date_to": date_to,
            "total": total,
            "distribution": distribution,
            "top10": distribution[:10],
            "trend": trend,
        }


async def get_classification_stats(date_from: str, date_to: str) -> Dict[str, Any]:
    """Get problem category + device_type classification statistics."""
    async with get_session() as session:
        from sqlalchemy import select, and_

        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to + "T23:59:59")

        _INVALID_TYPES = ["", "未知", "Analysis Complete", "分析完成", "分析总结",
                          "Unknown", "问题定位完成", "分析结果", "Completed", "Done", "N/A"]

        stmt = select(
            AnalysisRecord.problem_categories_json,
            AnalysisRecord.device_type,
            AnalysisRecord.problem_type,
        ).where(and_(
            AnalysisRecord.created_at >= start,
            AnalysisRecord.created_at <= end,
            AnalysisRecord.problem_type.notin_(_INVALID_TYPES),
        ))
        rows = (await session.execute(stmt)).fetchall()

        # Aggregate by category, subcategory, and device_type
        cat_counts: Dict[str, int] = {}        # category -> count
        subcat_counts: Dict[str, Dict[str, int]] = {}  # category -> {subcategory -> count}
        device_counts: Dict[str, int] = {}     # device_type -> count
        device_cat_counts: Dict[str, Dict[str, int]] = {}  # device_type -> {category -> count}
        total_with_categories = 0

        for row in rows:
            categories = []
            try:
                categories = json.loads(row.problem_categories_json or "[]")
            except Exception:
                pass

            device = normalize_device_type(row.device_type or "") or "未知"
            device_counts[device] = device_counts.get(device, 0) + 1

            if not categories:
                # Fallback: use problem_type as single category
                pt = row.problem_type or "其他"
                cat_counts[pt] = cat_counts.get(pt, 0) + 1
                if device not in device_cat_counts:
                    device_cat_counts[device] = {}
                device_cat_counts[device][pt] = device_cat_counts[device].get(pt, 0) + 1
                continue

            total_with_categories += 1
            seen_cats = set()
            for c in categories:
                cat = c.get("category", "其他") or "其他"
                subcat = c.get("subcategory", "") or ""

                if cat not in seen_cats:
                    cat_counts[cat] = cat_counts.get(cat, 0) + 1
                    seen_cats.add(cat)

                    if device not in device_cat_counts:
                        device_cat_counts[device] = {}
                    device_cat_counts[device][cat] = device_cat_counts[device].get(cat, 0) + 1

                if subcat:
                    if cat not in subcat_counts:
                        subcat_counts[cat] = {}
                    subcat_counts[cat][subcat] = subcat_counts[cat].get(subcat, 0) + 1

        # Sort everything
        sorted_cats = sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)
        sorted_devices = sorted(device_counts.items(), key=lambda x: x[1], reverse=True)

        category_distribution = []
        for cat, count in sorted_cats:
            subcats = subcat_counts.get(cat, {})
            sorted_subcats = sorted(subcats.items(), key=lambda x: x[1], reverse=True)
            category_distribution.append({
                "category": cat,
                "count": count,
                "subcategories": [{"subcategory": s, "count": c} for s, c in sorted_subcats],
            })

        device_distribution = []
        for dev, count in sorted_devices:
            cats = device_cat_counts.get(dev, {})
            sorted_dev_cats = sorted(cats.items(), key=lambda x: x[1], reverse=True)
            device_distribution.append({
                "device_type": dev,
                "count": count,
                "categories": [{"category": c, "count": n} for c, n in sorted_dev_cats],
            })

        return {
            "date_from": date_from,
            "date_to": date_to,
            "total": len(rows),
            "total_with_categories": total_with_categories,
            "category_distribution": category_distribution,
            "device_distribution": device_distribution,
        }


async def get_analyses_for_backfill(limit: int = 500) -> List[Dict[str, Any]]:
    """Get PRE-VOC-CUTOVER analyses that need legacy classification backfill (empty problem_categories_json AND empty voc_tags_json — see the guard comment below)."""
    async with get_session() as session:
        from sqlalchemy import select, or_

        _INVALID_TYPES = ["", "未知", "Analysis Complete", "分析完成", "分析总结",
                          "Unknown", "问题定位完成", "分析结果", "Completed", "Done", "N/A"]

        stmt = select(
            AnalysisRecord.id,
            AnalysisRecord.issue_id,
            AnalysisRecord.problem_type,
            AnalysisRecord.root_cause,
            AnalysisRecord.device_type,
            AnalysisRecord.problem_categories_json,
        ).where(
            AnalysisRecord.problem_type.notin_(_INVALID_TYPES),
            or_(
                AnalysisRecord.problem_categories_json == "[]",
                AnalysisRecord.problem_categories_json == "",
                AnalysisRecord.problem_categories_json.is_(None),
            ),
            # Guard added 2026-08: since save_analysis() stopped auto-
            # classifying, ALL new (VOC-era) rows also have empty
            # problem_categories_json. Without this second condition the
            # legacy /api/analytics/backfill-classifications button would
            # immediately re-classify brand-new VOC-tagged tickets with the
            # retired keyword system, un-freezing the field this feature
            # deliberately stopped touching. A row with any voc_tags_json is
            # definitionally post-cutover and must never match here.
            or_(
                AnalysisRecord.voc_tags_json == "[]",
                AnalysisRecord.voc_tags_json == "",
                AnalysisRecord.voc_tags_json.is_(None),
            ),
        ).order_by(AnalysisRecord.created_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).fetchall()
        return [{"id": r.id, "issue_id": r.issue_id, "problem_type": r.problem_type,
                 "root_cause": r.root_cause, "device_type": r.device_type} for r in rows]


async def update_analysis_classification(analysis_id: int, categories: list, device_type: str = ""):
    """Update classification fields on an existing analysis record."""
    async with get_session() as session:
        record = await session.get(AnalysisRecord, analysis_id)
        if record:
            record.problem_categories_json = json.dumps(categories, ensure_ascii=False)
            if device_type:
                record.device_type = device_type
            await session.commit()


# ---------------------------------------------------------------------------
# VOC Portal taxonomy — tag CRUD + classification read/write
#
# 新字段并存策略：problem_categories_json（旧分类）冻结只读，voc_tags_json（新分类）
# 独立写入，可随时对比迁移矩阵、可回滚。见 docs/superpowers/specs 对应设计文档。
# ---------------------------------------------------------------------------
def _voc_tag_to_dict(row: "VocTagRecord") -> Dict[str, Any]:
    return {
        "id": row.id,
        "level_1_category": row.level_1_category or "",
        "level_2_label": row.level_2_label or "",
        "level_3_diagnosis": row.level_3_diagnosis or "",
        "definition": row.definition or "",
        "positive_examples": json.loads(row.positive_examples_json or "[]"),
        "mece_rules": json.loads(row.mece_rules_json or "[]"),
        "negative_examples": json.loads(row.negative_examples_json or "[]"),
        "updated_by": row.updated_by or "",
        "retired": bool(row.retired),
    }


async def upsert_voc_tags(tags: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Upsert a full snapshot of *active* VOC tags (as returned by GET /api/taxonomy/tags).

    VOC 只返回 active tags，因此"这次同步没收到、但 DB 里已有"的 tag id 被视为已在
    VOC 侧 retire —— 本地打 retired=True（不再进新记录打标 prompt，已打标记录不受影响）。
    幂等：同一份 snapshot 重复跑，added/changed/retired 都应为空。
    """
    remote_ids = {t["id"] for t in tags}
    added: List[str] = []
    changed: List[str] = []
    retired: List[str] = []

    async with get_session() as session:
        from sqlalchemy import select

        existing_rows = (await session.execute(select(VocTagRecord))).scalars().all()
        existing_by_id = {r.id: r for r in existing_rows}

        for tag in tags:
            tag_id = tag["id"]
            positive_examples_json = json.dumps(tag.get("positive_examples") or [], ensure_ascii=False)
            mece_rules_json = json.dumps(tag.get("mece_rules") or [], ensure_ascii=False)
            negative_examples_json = json.dumps(tag.get("negative_examples") or [], ensure_ascii=False)

            row = existing_by_id.get(tag_id)
            if row is None:
                row = VocTagRecord(id=tag_id)
                session.add(row)
                added.append(tag_id)
            else:
                is_changed = (
                    row.level_1_category != (tag.get("level_1_category") or "")
                    or row.level_2_label != (tag.get("level_2_label") or "")
                    or row.level_3_diagnosis != (tag.get("level_3_diagnosis") or "")
                    or row.definition != (tag.get("definition") or "")
                    or row.positive_examples_json != positive_examples_json
                    or row.mece_rules_json != mece_rules_json
                    or row.negative_examples_json != negative_examples_json
                    or bool(row.retired)
                )
                if is_changed:
                    changed.append(tag_id)

            row.level_1_category = tag.get("level_1_category") or ""
            row.level_2_label = tag.get("level_2_label") or ""
            row.level_3_diagnosis = tag.get("level_3_diagnosis") or ""
            row.definition = tag.get("definition") or ""
            row.positive_examples_json = positive_examples_json
            row.mece_rules_json = mece_rules_json
            row.negative_examples_json = negative_examples_json
            row.updated_by = tag.get("updated_by") or ""
            row.retired = False
            row.synced_at = datetime.utcnow()

        for row in existing_rows:
            if row.id not in remote_ids and not row.retired:
                row.retired = True
                retired.append(row.id)

        await session.commit()

    return {"added": added, "changed": changed, "retired": retired}


async def get_voc_tags(include_retired: bool = False) -> List[Dict[str, Any]]:
    """All VOC tags from the local DB cache, optionally including retired ones."""
    async with get_session() as session:
        from sqlalchemy import select

        stmt = select(VocTagRecord)
        if not include_retired:
            stmt = stmt.where(VocTagRecord.retired.is_(False))
        rows = (await session.execute(stmt)).scalars().all()
        return [_voc_tag_to_dict(r) for r in rows]


async def get_analyses_for_voc_backfill(
    since: str, limit: int = 500, only_empty: bool = True,
) -> List[Dict[str, Any]]:
    """Analyses created since `since` (YYYY-MM-DD) that need VOC tag backfill.

    Joins issues for description/category — the classifier's evidence package
    needs the original ticket text, not just the AI analysis conclusion.
    only_empty=True (default) skips analyses that already have voc_tags_json —
    makes the backfill script naturally idempotent / resumable.
    """
    async with get_session() as session:
        from sqlalchemy import select, or_, and_

        start = datetime.fromisoformat(since)
        conditions = [AnalysisRecord.created_at >= start]
        if only_empty:
            conditions.append(or_(
                AnalysisRecord.voc_tags_json == "[]",
                AnalysisRecord.voc_tags_json == "",
                AnalysisRecord.voc_tags_json.is_(None),
            ))

        stmt = (
            select(
                AnalysisRecord.id,
                AnalysisRecord.issue_id,
                AnalysisRecord.problem_type,
                AnalysisRecord.problem_type_en,
                AnalysisRecord.root_cause,
                AnalysisRecord.root_cause_en,
                AnalysisRecord.device_type,
                AnalysisRecord.platform,
                IssueRecord.description,
                IssueRecord.category,
            )
            .outerjoin(IssueRecord, IssueRecord.id == AnalysisRecord.issue_id)
            .where(and_(*conditions))
            .order_by(AnalysisRecord.created_at.asc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).fetchall()
        return [
            {
                "analysis_id": r.id,
                "issue_id": r.issue_id,
                "problem_type": r.problem_type or "",
                "problem_type_en": r.problem_type_en or "",
                "root_cause": r.root_cause or "",
                "root_cause_en": r.root_cause_en or "",
                "device_type": r.device_type or "",
                "platform": r.platform or "",
                "description": r.description or "",
                "category": r.category or "",
            }
            for r in rows
        ]


async def update_analysis_voc_tags(analysis_id: int, tags: List[Dict[str, Any]]) -> bool:
    """Write VOC tags onto an existing analysis. Returns False if the row doesn't exist."""
    async with get_session() as session:
        record = await session.get(AnalysisRecord, analysis_id)
        if not record:
            return False
        record.voc_tags_json = json.dumps(tags, ensure_ascii=False)
        await session.commit()
        return True


async def get_voc_classification_stats(
    date_from: str, date_to: str, include_secondary: bool = False,
) -> Dict[str, Any]:
    """Three-level (group → label → diagnosis) VOC classification stats for a date range.

    Counts only the primary tag by default to avoid double-counting a ticket across
    both a pie chart and a drill-down tree (an analyses row usually has 1 primary +
    up to 2 secondary tags). include_secondary=True folds secondary tags into the
    same tree for a "where does this diagnosis co-occur" view.
    """
    async with get_session() as session:
        from sqlalchemy import select, and_

        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to + "T23:59:59")

        stmt = select(AnalysisRecord.voc_tags_json).where(and_(
            AnalysisRecord.created_at >= start,
            AnalysisRecord.created_at <= end,
        ))
        rows = (await session.execute(stmt)).fetchall()

        # tree[group][label][diagnosis] = count
        tree: Dict[str, Dict[str, Dict[str, int]]] = {}
        group_counts: Dict[str, int] = {}
        total_tagged = 0

        def _add(tag: Dict[str, Any]) -> None:
            group = tag.get("level_1_category") or "未分类"
            label = tag.get("level_2_label") or ""
            diagnosis = tag.get("level_3_diagnosis") or ""
            group_counts[group] = group_counts.get(group, 0) + 1
            tree.setdefault(group, {}).setdefault(label, {})
            tree[group][label][diagnosis] = tree[group][label].get(diagnosis, 0) + 1

        for row in rows:
            try:
                tags = json.loads(row.voc_tags_json or "[]")
            except Exception:
                continue
            if not tags:
                continue
            total_tagged += 1
            for tag in tags:
                if tag.get("role") == "primary":
                    _add(tag)
                elif include_secondary and tag.get("role") == "secondary":
                    _add(tag)

        groups = []
        for group, count in sorted(group_counts.items(), key=lambda x: x[1], reverse=True):
            labels = []
            for label, diag_counts in tree.get(group, {}).items():
                diagnoses = [
                    {"diagnosis": d, "count": c}
                    for d, c in sorted(diag_counts.items(), key=lambda x: x[1], reverse=True)
                ]
                labels.append({
                    "label": label,
                    "count": sum(diag_counts.values()),
                    "diagnoses": diagnoses,
                })
            labels.sort(key=lambda x: x["count"], reverse=True)
            groups.append({"group": group, "count": count, "labels": labels})

        return {
            "date_from": date_from,
            "date_to": date_to,
            "total": len(rows),
            "total_tagged": total_tagged,
            "groups": groups,
        }


async def get_voc_analysis_rows(date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Raw per-analysis rows for VOC trend/movers/digest aggregation
    (app.services.voc_digest) — one row per analysis in [date_from, date_to]
    (inclusive, same local-date granularity as get_voc_classification_stats).
    Callers parse voc_tags_json themselves via voc_digest._primary_tag; this
    function is just the DB round-trip.
    """
    async with get_session() as session:
        from sqlalchemy import select, and_

        start = datetime.fromisoformat(date_from)
        end = datetime.fromisoformat(date_to + "T23:59:59")

        stmt = select(
            AnalysisRecord.issue_id,
            AnalysisRecord.created_at,
            AnalysisRecord.voc_tags_json,
            AnalysisRecord.problem_type,
            AnalysisRecord.root_cause,
            AnalysisRecord.device_type,
            AnalysisRecord.platform,
            AnalysisRecord.needs_engineer,
        ).where(and_(
            AnalysisRecord.created_at >= start,
            AnalysisRecord.created_at <= end,
        ))
        rows = (await session.execute(stmt)).fetchall()
        return [
            {
                "issue_id": r.issue_id,
                "created_at": r.created_at,
                "voc_tags_json": r.voc_tags_json,
                "problem_type": r.problem_type,
                "root_cause": r.root_cause,
                "device_type": r.device_type,
                "platform": r.platform,
                "needs_engineer": bool(r.needs_engineer),
            }
            for r in rows
        ]


def _voc_weekly_digest_to_dict(row: "VocWeeklyDigest") -> Dict[str, Any]:
    return {
        "week_start": row.week_start,
        "stats": json.loads(row.stats_json or "{}"),
        "narrative": json.loads(row.narrative_json or "null"),
        "markdown": row.markdown or "",
        "model": row.model or "",
        "total_tokens": row.total_tokens or 0,
        "total_cost_usd": row.total_cost_usd or 0.0,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
    }


async def get_voc_weekly_digest(week_start: str) -> Optional[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(VocWeeklyDigest).where(VocWeeklyDigest.week_start == week_start)
        )).scalar_one_or_none()
        return _voc_weekly_digest_to_dict(row) if row else None


async def list_voc_weekly_digests(limit: int = 12) -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(VocWeeklyDigest).order_by(VocWeeklyDigest.week_start.desc()).limit(limit)
        )).scalars().all()
        return [_voc_weekly_digest_to_dict(r) for r in rows]


async def upsert_voc_weekly_digest(
    week_start: str, stats: Dict[str, Any], narrative: Optional[Dict[str, Any]],
    markdown: str, model: str = "", total_tokens: int = 0, total_cost_usd: float = 0.0,
) -> Dict[str, Any]:
    async with get_session() as session:
        from sqlalchemy import select
        row = (await session.execute(
            select(VocWeeklyDigest).where(VocWeeklyDigest.week_start == week_start)
        )).scalar_one_or_none()
        if row is None:
            row = VocWeeklyDigest(week_start=week_start)
            session.add(row)
        row.stats_json = json.dumps(stats, ensure_ascii=False)
        row.narrative_json = json.dumps(narrative, ensure_ascii=False) if narrative is not None else "null"
        row.markdown = markdown
        row.model = model
        row.total_tokens = total_tokens
        row.total_cost_usd = total_cost_usd
        row.generated_at = datetime.utcnow()
        await session.commit()
        return _voc_weekly_digest_to_dict(row)


# ---------------------------------------------------------------------------
# Golden Samples CRUD
# ---------------------------------------------------------------------------
async def add_golden_sample(data: Dict[str, Any]) -> GoldenSampleRecord:
    async with get_session() as session:
        record = GoldenSampleRecord(
            issue_id=data.get("issue_id", ""),
            analysis_id=data.get("analysis_id", 0),
            problem_type=data.get("problem_type", ""),
            description=data.get("description", ""),
            root_cause=data.get("root_cause", ""),
            user_reply=data.get("user_reply", ""),
            confidence=data.get("confidence", "high"),
            rule_type=data.get("rule_type", ""),
            tags_json=json.dumps(data.get("tags", []), ensure_ascii=False),
            quality=data.get("quality", "verified"),
            created_by=data.get("created_by", ""),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def list_golden_samples(rule_type: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(GoldenSampleRecord).order_by(GoldenSampleRecord.created_at.desc())
        if rule_type:
            stmt = stmt.where(GoldenSampleRecord.rule_type == rule_type)
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return [_golden_sample_to_dict(r) for r in result.scalars().all()]


async def get_golden_sample(sample_id: int) -> Optional[Dict[str, Any]]:
    async with get_session() as session:
        record = await session.get(GoldenSampleRecord, sample_id)
        if not record:
            return None
        return _golden_sample_to_dict(record)


async def delete_golden_sample(sample_id: int) -> bool:
    async with get_session() as session:
        record = await session.get(GoldenSampleRecord, sample_id)
        if record:
            await session.delete(record)
            await session.commit()
            return True
        return False


async def get_golden_samples_stats() -> Dict[str, Any]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(GoldenSampleRecord)
        result = await session.execute(stmt)
        samples = list(result.scalars().all())
        by_rule: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        for s in samples:
            rt = s.rule_type or "unknown"
            by_rule[rt] = by_rule.get(rt, 0) + 1
            pt = s.problem_type or "unknown"
            by_type[pt] = by_type.get(pt, 0) + 1
        return {"total": len(samples), "by_rule_type": by_rule, "by_problem_type": by_type}


def _golden_sample_to_dict(r: GoldenSampleRecord) -> Dict[str, Any]:
    return {
        "id": r.id,
        "issue_id": r.issue_id or "",
        "analysis_id": r.analysis_id or 0,
        "problem_type": r.problem_type or "",
        "description": r.description or "",
        "root_cause": r.root_cause or "",
        "user_reply": r.user_reply or "",
        "confidence": r.confidence or "high",
        "rule_type": r.rule_type or "",
        "tags": json.loads(r.tags_json) if r.tags_json else [],
        "quality": r.quality or "verified",
        "created_by": r.created_by or "",
        "created_at": (r.created_at.isoformat() + "Z") if r.created_at else "",
    }


# ---------------------------------------------------------------------------
# Eval CRUD
# ---------------------------------------------------------------------------
async def create_eval_dataset(data: Dict[str, Any]) -> EvalDatasetRecord:
    async with get_session() as session:
        record = EvalDatasetRecord(
            name=data.get("name", ""),
            description=data.get("description", ""),
            sample_ids_json=json.dumps(data.get("sample_ids", []), ensure_ascii=False),
            created_by=data.get("created_by", ""),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def list_eval_datasets() -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(EvalDatasetRecord).order_by(EvalDatasetRecord.created_at.desc())
        result = await session.execute(stmt)
        return [{
            "id": r.id,
            "name": r.name or "",
            "description": r.description or "",
            "sample_ids": json.loads(r.sample_ids_json) if r.sample_ids_json else [],
            "created_by": r.created_by or "",
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else "",
        } for r in result.scalars().all()]


async def get_eval_dataset(dataset_id: int) -> Optional[Dict[str, Any]]:
    async with get_session() as session:
        record = await session.get(EvalDatasetRecord, dataset_id)
        if not record:
            return None
        return {
            "id": record.id,
            "name": record.name or "",
            "description": record.description or "",
            "sample_ids": json.loads(record.sample_ids_json) if record.sample_ids_json else [],
            "created_by": record.created_by or "",
            "created_at": (record.created_at.isoformat() + "Z") if record.created_at else "",
        }


async def create_eval_run(data: Dict[str, Any]) -> EvalRunRecord:
    async with get_session() as session:
        record = EvalRunRecord(
            dataset_id=data.get("dataset_id", 0),
            status="pending",
            config_json=json.dumps(data.get("config", {}), ensure_ascii=False),
            created_by=data.get("created_by", ""),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def update_eval_run(run_id: int, **kwargs):
    async with get_session() as session:
        record = await session.get(EvalRunRecord, run_id)
        if not record:
            return
        for key, value in kwargs.items():
            if key == "results":
                record.results_json = json.dumps(value, ensure_ascii=False)
            elif key == "summary":
                record.summary_json = json.dumps(value, ensure_ascii=False)
            elif hasattr(record, key):
                setattr(record, key, value)
        await session.commit()


async def list_eval_runs(dataset_id: Optional[int] = None) -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(EvalRunRecord).order_by(EvalRunRecord.created_at.desc())
        if dataset_id:
            stmt = stmt.where(EvalRunRecord.dataset_id == dataset_id)
        result = await session.execute(stmt)
        return [{
            "id": r.id,
            "dataset_id": r.dataset_id,
            "status": r.status or "pending",
            "config": json.loads(r.config_json) if r.config_json else {},
            "results": json.loads(r.results_json) if r.results_json else [],
            "summary": json.loads(r.summary_json) if r.summary_json else {},
            "started_at": (r.started_at.isoformat() + "Z") if r.started_at else None,
            "finished_at": (r.finished_at.isoformat() + "Z") if r.finished_at else None,
            "created_by": r.created_by or "",
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else "",
        } for r in result.scalars().all()]


async def get_eval_run(run_id: int) -> Optional[Dict[str, Any]]:
    async with get_session() as session:
        record = await session.get(EvalRunRecord, run_id)
        if not record:
            return None
        return {
            "id": record.id,
            "dataset_id": record.dataset_id,
            "status": record.status or "pending",
            "config": json.loads(record.config_json) if record.config_json else {},
            "results": json.loads(record.results_json) if record.results_json else [],
            "summary": json.loads(record.summary_json) if record.summary_json else {},
            "started_at": (record.started_at.isoformat() + "Z") if record.started_at else None,
            "finished_at": (record.finished_at.isoformat() + "Z") if record.finished_at else None,
            "created_by": record.created_by or "",
            "created_at": (record.created_at.isoformat() + "Z") if record.created_at else "",
        }
