"""
Database layer using SQLAlchemy async with SQLite/PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, Integer, String, Text, Boolean, Float, func, text
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

    id = Column(String(64), primary_key=True)              # Feishu record_id
    description = Column(Text, default="")
    device_sn = Column(String(64), default="")
    firmware = Column(String(32), default="")
    app_version = Column(String(32), default="")
    priority = Column(String(4), default="")
    zendesk = Column(String(256), default="")
    zendesk_id = Column(String(32), default="")
    feishu_link = Column(String(512), default="")
    source = Column(String(32), default="feishu")         # feishu / user_upload
    log_files_json = Column(Text, default="[]")            # JSON array
    status = Column(String(32), default="pending")         # pending / analyzing / done / failed
    rule_type = Column(String(64), default="")
    created_at_ms = Column(Integer, default=0)             # Feishu creation time (Unix ms)
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
    source = Column(String(32), default="feishu")
    workspace_path = Column(Text, default="")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AnalysisRecord(Base):
    __tablename__ = "analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), index=True)
    issue_id = Column(String(64), index=True)
    problem_type = Column(String(128), default="")
    root_cause = Column(Text, default="")
    confidence = Column(String(16), default="medium")
    confidence_reason = Column(Text, default="")
    key_evidence_json = Column(Text, default="[]")
    core_logs_json = Column(Text, default="[]")
    code_locations_json = Column(Text, default="[]")
    user_reply = Column(Text, default="")
    needs_engineer = Column(Boolean, default=False)
    requires_more_info = Column(Boolean, default=False)
    more_info_guidance = Column(Text, default="")
    next_steps_json = Column(Text, default="[]")
    fix_suggestion = Column(Text, default="")
    rule_type = Column(String(64), default="")
    agent_type = Column(String(32), default="")
    raw_output = Column(Text, default="")
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

    _engine = create_async_engine(db_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in db_url:
            await _ensure_sqlite_columns(conn)


async def _ensure_sqlite_columns(conn):
    """Best-effort schema evolution for existing local SQLite databases."""
    migrations: Dict[str, Dict[str, str]] = {
        "issues": {
            "source": "TEXT DEFAULT 'feishu'",
        },
        "tasks": {
            "source": "TEXT DEFAULT 'feishu'",
            "workspace_path": "TEXT DEFAULT ''",
        },
        "analyses": {
            "core_logs_json": "TEXT DEFAULT '[]'",
            "code_locations_json": "TEXT DEFAULT '[]'",
            "requires_more_info": "BOOLEAN DEFAULT 0",
            "more_info_guidance": "TEXT DEFAULT ''",
            "next_steps_json": "TEXT DEFAULT '[]'",
        },
    }

    for table, cols in migrations.items():
        existing = await _sqlite_columns(conn, table)
        for col, ddl in cols.items():
            if col in existing:
                continue
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


async def _sqlite_columns(conn, table: str) -> set[str]:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    names = set()
    for row in result.fetchall():
        try:
            names.add(row[1])
        except Exception:
            pass
    return names


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
        # Use merge to handle concurrent inserts safely
        record = IssueRecord(
            id=rid,
            description=data.get("description", ""),
            device_sn=data.get("device_sn", ""),
            firmware=data.get("firmware", ""),
            app_version=data.get("app_version", ""),
            priority=data.get("priority", ""),
            zendesk=data.get("zendesk", ""),
            zendesk_id=data.get("zendesk_id", ""),
            feishu_link=data.get("feishu_link", ""),
            source=data.get("source", "feishu"),
            created_at_ms=data.get("created_at_ms", 0),
            log_files_json=json.dumps(data.get("log_files", []), ensure_ascii=False),
            status=status,
            updated_at=datetime.utcnow(),
        )
        merged = await session.merge(record)
        await session.commit()
        return merged


async def update_issue_status(issue_id: str, status: str):
    async with get_session() as session:
        record = await session.get(IssueRecord, issue_id)
        if record:
            record.status = status
            record.updated_at = datetime.utcnow()
            await session.commit()


async def create_task(
    task_id: str,
    issue_id: str,
    agent_type: str = "",
    source: str = "feishu",
    workspace_path: str = "",
) -> TaskRecord:
    async with get_session() as session:
        record = TaskRecord(
            id=task_id,
            issue_id=issue_id,
            agent_type=agent_type,
            source=source,
            workspace_path=workspace_path,
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
            root_cause=data.get("root_cause", ""),
            confidence=data.get("confidence", "medium"),
            confidence_reason=data.get("confidence_reason", ""),
            key_evidence_json=json.dumps(data.get("key_evidence", []), ensure_ascii=False),
            core_logs_json=json.dumps(data.get("core_logs", []), ensure_ascii=False),
            code_locations_json=json.dumps(data.get("code_locations", []), ensure_ascii=False),
            user_reply=data.get("user_reply", ""),
            needs_engineer=data.get("needs_engineer", False),
            requires_more_info=data.get("requires_more_info", False),
            more_info_guidance=data.get("more_info_guidance", ""),
            next_steps_json=json.dumps(data.get("next_steps", []), ensure_ascii=False),
            fix_suggestion=data.get("fix_suggestion", ""),
            rule_type=data.get("rule_type", ""),
            agent_type=data.get("agent_type", ""),
            raw_output=data.get("raw_output", ""),
        )
        session.add(record)
        await session.commit()
        return record


async def get_analysis_by_task(task_id: str) -> Optional[AnalysisRecord]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(AnalysisRecord).where(
            AnalysisRecord.task_id == task_id
        ).order_by(AnalysisRecord.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_analysis_by_issue(issue_id: str) -> Optional[AnalysisRecord]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(AnalysisRecord).where(
            AnalysisRecord.issue_id == issue_id
        ).order_by(AnalysisRecord.created_at.desc()).limit(1)
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
    Excludes analyzing, failed (shown in 进行中), and done (shown in 已完成).
    """
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(IssueRecord.id).where(
            IssueRecord.status.in_(["analyzing", "failed", "done"])
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
        status_filter = IssueRecord.status.in_(statuses)

        # Count total
        count_stmt = select(func.count()).select_from(IssueRecord).where(status_filter)
        total = (await session.execute(count_stmt)).scalar() or 0

        # Get page
        offset = (page - 1) * page_size
        stmt = select(IssueRecord).where(
            status_filter
        ).order_by(IssueRecord.updated_at.desc()).offset(offset).limit(page_size)
        issues = list((await session.execute(stmt)).scalars().all())

        # Enrich with analysis/task data
        items = []
        for issue in issues:
            # Always try to load both task and analysis for any status
            a_stmt = select(AnalysisRecord).where(
                AnalysisRecord.issue_id == issue.id
            ).order_by(AnalysisRecord.created_at.desc()).limit(1)
            analysis = (await session.execute(a_stmt)).scalar_one_or_none()

            t_stmt = select(TaskRecord).where(
                TaskRecord.issue_id == issue.id
            ).order_by(TaskRecord.created_at.desc()).limit(1)
            task = (await session.execute(t_stmt)).scalar_one_or_none()

            items.append(_issue_to_dict(issue, analysis=analysis, task=task))

        return items, total


def _issue_to_dict(
    issue: IssueRecord,
    analysis: Optional[AnalysisRecord] = None,
    task: Optional[TaskRecord] = None,
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
        "feishu_link": issue.feishu_link or "",
        "feishu_status": issue.status or "pending",
        "result_summary": "",
        "root_cause_summary": "",
        "created_at_ms": issue.created_at_ms or 0,
        "log_files": json.loads(issue.log_files_json) if issue.log_files_json else [],
        "local_status": issue.status,  # our own tracking
    }

    if analysis:
        d["analysis"] = {
            "task_id": analysis.task_id,
            "issue_id": analysis.issue_id,
            "problem_type": analysis.problem_type or "",
            "root_cause": analysis.root_cause or "",
            "confidence": analysis.confidence or "medium",
            "confidence_reason": analysis.confidence_reason or "",
            "key_evidence": json.loads(analysis.key_evidence_json) if analysis.key_evidence_json else [],
            "core_logs": json.loads(analysis.core_logs_json) if analysis.core_logs_json else [],
            "code_locations": json.loads(analysis.code_locations_json) if analysis.code_locations_json else [],
            "user_reply": analysis.user_reply or "",
            "needs_engineer": analysis.needs_engineer,
            "requires_more_info": analysis.requires_more_info,
            "more_info_guidance": analysis.more_info_guidance or "",
            "next_steps": json.loads(analysis.next_steps_json) if analysis.next_steps_json else [],
            "fix_suggestion": analysis.fix_suggestion or "",
            "rule_type": analysis.rule_type or "",
            "agent_type": analysis.agent_type or "",
            "created_at": analysis.created_at.isoformat() if analysis.created_at else "",
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
