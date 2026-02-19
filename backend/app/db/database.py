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
    status = Column(String(32), default="pending")         # pending / analyzing / done / failed / deleted
    rule_type = Column(String(64), default="")
    platform = Column(String(16), default="")              # APP / Web / Desktop
    category = Column(String(128), default="")             # problem category
    created_by = Column(String(64), default="")            # username who triggered analysis
    deleted = Column(Boolean, default=False)
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
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE analyses ADD COLUMN {col} {coltype} DEFAULT {default}"))
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
            created_at_ms=data.get("created_at_ms", 0),
            log_files_json=json.dumps(data.get("log_files", []), ensure_ascii=False),
            status=status,
            updated_at=datetime.utcnow(),
        )
        session.add(record)
        await session.commit()
        return record


async def update_issue_status(issue_id: str, status: str):
    async with get_session() as session:
        record = await session.get(IssueRecord, issue_id)
        if record:
            record.status = status
            record.updated_at = datetime.utcnow()
            await session.commit()


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
        if record and not record.created_by:
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
            IssueRecord.status.in_(["analyzing", "failed", "done"]),
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


async def get_tracked_issues_paginated(
    page: int = 1,
    page_size: int = 20,
    created_by: Optional[str] = None,
    platform: Optional[str] = None,
    category: Optional[str] = None,
    status_filter: Optional[str] = None,
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

        items = []
        for issue in issues:
            a_stmt = select(AnalysisRecord).where(AnalysisRecord.issue_id == issue.id).order_by(AnalysisRecord.created_at.desc()).limit(1)
            analysis = (await session.execute(a_stmt)).scalar_one_or_none()
            t_stmt = select(TaskRecord).where(TaskRecord.issue_id == issue.id).order_by(TaskRecord.created_at.desc()).limit(1)
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
        "created_at": issue.created_at.isoformat() if issue.created_at else "",
    }

    if analysis:
        d["analysis"] = {
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


async def list_users() -> List[Dict[str, Any]]:
    async with get_session() as session:
        from sqlalchemy import select
        stmt = select(UserRecord).order_by(UserRecord.created_at)
        result = await session.execute(stmt)
        return [{"username": r.username, "role": r.role, "feishu_email": r.feishu_email} for r in result.scalars().all()]


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

        # Fail reasons
        fail_stmt = select(EventRecord.detail_json).where(
            date_filter, EventRecord.event_type == "analysis_fail"
        ).limit(50)
        fail_details = []
        for row in (await session.execute(fail_stmt)).fetchall():
            try:
                fail_details.append(json.loads(row[0]))
            except Exception:
                pass

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

        # Top users
        top_users_stmt = select(
            EventRecord.username, func.count()
        ).where(date_filter, EventRecord.username != "").group_by(
            EventRecord.username
        ).order_by(func.count().desc()).limit(10)
        top_users = [{"username": row[0], "count": row[1]} for row in (await session.execute(top_users_stmt)).fetchall()]

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
            "failed_analyses": type_counts.get("analysis_fail", 0),
            "feedback_submitted": type_counts.get("feedback_submit", 0),
            "escalations": type_counts.get("escalate", 0),
        }
