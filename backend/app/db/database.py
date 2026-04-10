"""
Database layer using SQLAlchemy async with SQLite/PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, Integer, String, Text, Boolean, Float, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


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
    fix_suggestion = Column(Text, default="")
    rule_type = Column(String(64), default="")
    agent_type = Column(String(32), default="")
    raw_output = Column(Text, default="")
    followup_question = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class EventRecord(Base):
    """Core analytics events table."""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), index=True)      # analysis_start, analysis_done, analysis_fail, feedback_submit, page_visit, escalate
    issue_id = Column(String(64), default="")
    username = Column(String(64), default="")
    detail_json = Column(Text, default="{}")           # flexible payload
    duration_ms = Column(Integer, default=0)           # for timed events (analysis duration)
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

    # SQLite needs WAL mode + busy_timeout to handle concurrent async writes
    connect_args = {}
    pool_kwargs = {}
    if "sqlite" in db_url:
        connect_args = {"timeout": 30}  # seconds to wait for lock
        pool_kwargs = {
            "pool_size": 1,       # SQLite only supports 1 writer
            "max_overflow": 4,    # queue extra connections instead of failing
            "pool_timeout": 30,   # wait up to 30s for a pool slot
        }

    _engine = create_async_engine(
        db_url,
        echo=False,
        connect_args=connect_args,
        **pool_kwargs,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # Enable WAL mode for SQLite (allows concurrent reads while writing)
    if "sqlite" in db_url:
        async with _engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
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
                existing.updated_at = datetime.utcnow()
                await session.commit()
                return existing
            raise
        return record


async def update_issue_status(issue_id: str, status: str):
    async with get_session() as session:
        record = await session.get(IssueRecord, issue_id)
        if record:
            record.status = status
            record.updated_at = datetime.utcnow()
            await session.commit()


async def escalate_issue(issue_id: str, escalated_by: str = "", note: str = "") -> bool:
    async with get_session() as session:
        record = await session.get(IssueRecord, issue_id)
        if not record:
            return False
        record.status = "escalated"
        record.escalated_at = datetime.utcnow()
        record.escalated_by = escalated_by
        record.escalation_note = note
        record.updated_at = datetime.utcnow()
        await session.commit()
        return True


async def soft_delete_issue(issue_id: str) -> bool:
    async with get_session() as session:
        record = await session.get(IssueRecord, issue_id)
        if record:
            record.deleted = True
            record.updated_at = datetime.utcnow()
            await session.commit()
            return True
        return False


async def set_issue_created_by(issue_id: str, username: str):
    async with get_session() as session:
        record = await session.get(IssueRecord, issue_id)
        if record and username:
            record.created_by = username
            await session.commit()


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


async def save_analysis(data: Dict[str, Any]) -> AnalysisRecord:
    async with get_session() as session:
        record = AnalysisRecord(
            task_id=data.get("task_id", ""),
            issue_id=data.get("issue_id", ""),
            problem_type=data.get("problem_type", ""),
            problem_type_en=data.get("problem_type_en", ""),
            root_cause=data.get("root_cause", ""),
            root_cause_en=data.get("root_cause_en", ""),
            confidence=data.get("confidence", "medium"),
            confidence_reason=data.get("confidence_reason", ""),
            key_evidence_json=json.dumps(data.get("key_evidence", []), ensure_ascii=False),
            user_reply=data.get("user_reply", ""),
            user_reply_en=data.get("user_reply_en", ""),
            needs_engineer=data.get("needs_engineer", False),
            fix_suggestion=data.get("fix_suggestion", ""),
            rule_type=data.get("rule_type", ""),
            agent_type=data.get("agent_type", ""),
            raw_output=data.get("raw_output", ""),
            followup_question=data.get("followup_question", ""),
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
    """
    async with get_session() as session:
        from sqlalchemy import select, func, and_

        conditions = [IssueRecord.deleted == False, IssueRecord.status != "pending"]
        if created_by:
            conditions.append(IssueRecord.created_by == created_by)
        if platform:
            conditions.append(IssueRecord.platform == platform)
        if category:
            conditions.append(IssueRecord.category.contains(category))
        if status_filter:
            conditions.append(IssueRecord.status == status_filter)
        if source:
            conditions.append(IssueRecord.source == source)
        if zendesk_id:
            conditions.append(IssueRecord.zendesk_id.contains(zendesk_id.strip("#")))
        if date_from:
            conditions.append(IssueRecord.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            conditions.append(IssueRecord.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))

        where = and_(*conditions)

        count_stmt = select(func.count()).select_from(IssueRecord).where(where)
        total = (await session.execute(count_stmt)).scalar() or 0

        offset = (page - 1) * page_size
        stmt = select(IssueRecord).where(where).order_by(IssueRecord.updated_at.desc()).offset(offset).limit(page_size)
        issues = list((await session.execute(stmt)).scalars().all())

        items = await _enrich_issues_batch(session, issues)
        return items, total


async def _enrich_issues_batch(
    session: AsyncSession,
    issues: List[IssueRecord],
) -> List[Dict[str, Any]]:
    """Batch-load analysis + task data for a list of issues.

    Uses 2 batch queries (analysis + count) + per-issue task lookup
    within the same session to avoid N+1 session overhead.
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
    issue: IssueRecord,
    analysis: Optional[AnalysisRecord] = None,
    task: Optional[TaskRecord] = None,
    analysis_count: int = 0,
) -> Dict[str, Any]:
    """Convert DB records to a dict matching the frontend Issue+Result shape."""
    d: Dict[str, Any] = {
        "record_id": issue.id,
        "description": issue.description or "",
        "device_sn": issue.device_sn or "",
        "firmware": issue.firmware or "",
        "app_version": issue.app_version or "",
        "priority": issue.priority or "",
        "zendesk": issue.zendesk or "",
        "zendesk_id": issue.zendesk_id or "",
        "source": issue.source or "feishu",
        "feishu_link": issue.feishu_link or "",
        "feishu_status": issue.status or "pending",
        "linear_issue_id": issue.linear_issue_id or "",
        "linear_issue_url": issue.linear_issue_url or "",
        "result_summary": "",
        "root_cause_summary": "",
        "created_at_ms": issue.created_at_ms or 0,
        "log_files": json.loads(issue.log_files_json) if issue.log_files_json else [],
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
            "fix_suggestion": analysis.fix_suggestion or "",
            "rule_type": analysis.rule_type or "",
            "agent_type": analysis.agent_type or "",
            "followup_question": analysis.followup_question or "",
            "created_at": (analysis.created_at.isoformat() + "Z") if analysis.created_at else "",
        }
        d["result_summary"] = analysis.user_reply or ""
        d["root_cause_summary"] = analysis.root_cause or ""

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


async def upsert_user(username: str, feishu_email: str = "") -> Dict[str, Any]:
    async with get_session() as session:
        record = UserRecord(
            username=username,
            role="admin" if username == ADMIN_USERNAME else "user",
            feishu_email=feishu_email,
        )
        merged = await session.merge(record)
        await session.commit()
        return {"username": merged.username, "role": merged.role, "feishu_email": merged.feishu_email}


async def get_user(username: str) -> Optional[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        record = await session.get(UserRecord, username)
        if not record:
            return None
        return {"username": record.username, "role": record.role, "feishu_email": record.feishu_email}


async def get_or_create_user(username: str) -> Dict[str, Any]:
    user = await get_user(username)
    if user:
        return user
    return await upsert_user(username)


async def touch_user_active(username: str):
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


async def get_current_oncall() -> List[str]:
    """Get the current week's oncall members based on rotation."""
    groups = await get_oncall_groups()
    if not groups:
        return []
    start_date_str = await get_oncall_config("start_date", "")
    if not start_date_str:
        return groups[0]["members"] if groups else []
    from datetime import date
    try:
        start = date.fromisoformat(start_date_str)
        today = date.today()
        weeks_elapsed = (today - start).days // 7
        idx = weeks_elapsed % len(groups)
        return groups[idx]["members"]
    except Exception:
        return groups[0]["members"] if groups else []


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
):
    """Log an analytics event."""
    async with get_session() as session:
        session.add(EventRecord(
            event_type=event_type,
            issue_id=issue_id,
            username=username,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
            duration_ms=duration_ms,
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
            "external_failures": external_fail_count,
            "feedback_submitted": type_counts.get("feedback_submit", 0),
            "escalations": type_counts.get("escalate", 0),
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
