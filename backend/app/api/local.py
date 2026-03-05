"""
API routes for locally-tracked issues (analyzed by Jarvis).

- 进行中: issues.status = 'analyzing'
- 已完成: issues.status = 'done'
- 失败:   issues.status = 'failed'
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import FileResponse

from app.config import get_settings
from app.db import database as db

logger = logging.getLogger("jarvis.api.local")
router = APIRouter()


@router.get("/in-progress")
async def list_in_progress(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues currently being analyzed (only 'analyzing' status)."""
    items, total = await db.get_local_issues_paginated("analyzing", page, page_size)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/completed")
async def list_completed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues where AI analysis finished (success or failure)."""
    items, total = await db.get_local_issues_paginated("done,failed", page, page_size)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/failed")
async def list_failed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues where analysis failed (from local DB)."""
    items, total = await db.get_local_issues_paginated("failed", page, page_size)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/tracking")
async def list_all_tracked(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    created_by: Optional[str] = Query(None, description="Filter by creator"),
    platform: Optional[str] = Query(None, description="Filter by platform: APP/Web/Desktop"),
    category: Optional[str] = Query(None, description="Filter by problem category (partial match)"),
    status: Optional[str] = Query(None, alias="status", description="Filter by status: analyzing/done/failed"),
    source: str = Query("", description="来源: feishu / local / linear / api"),
    date_from: Optional[str] = Query(None, description="From date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="To date YYYY-MM-DD"),
):
    """List ALL locally-tracked issues with multi-filter support."""
    items, total = await db.get_tracked_issues_paginated(
        page, page_size,
        created_by=created_by, platform=platform, category=category,
        status_filter=status, source=source or None, date_from=date_from, date_to=date_to,
    )
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/inaccurate")
async def list_inaccurate(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues marked as inaccurate."""
    items, total = await db.get_local_issues_paginated("inaccurate", page, page_size)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/{issue_id}/analyses")
async def get_issue_analyses(issue_id: str):
    """Get ALL analyses for an issue, ordered newest first."""
    import json as _json
    analyses = await db.get_all_analyses_by_issue(issue_id)
    return [
        {
            "task_id": a.task_id,
            "issue_id": a.issue_id,
            "problem_type": a.problem_type or "",
            "problem_type_en": a.problem_type_en or "",
            "root_cause": a.root_cause or "",
            "root_cause_en": a.root_cause_en or "",
            "confidence": a.confidence or "medium",
            "confidence_reason": a.confidence_reason or "",
            "key_evidence": _json.loads(a.key_evidence_json) if a.key_evidence_json else [],
            "user_reply": a.user_reply or "",
            "user_reply_en": a.user_reply_en or "",
            "needs_engineer": a.needs_engineer,
            "fix_suggestion": a.fix_suggestion or "",
            "rule_type": a.rule_type or "",
            "agent_type": a.agent_type or "",
            "followup_question": a.followup_question or "",
            "created_at": (a.created_at.isoformat() + "Z") if a.created_at else "",
        }
        for a in analyses
    ]


@router.get("/{issue_id}/detail")
async def get_issue_detail(issue_id: str):
    """Get a single issue with its analysis and task data by ID."""
    import json as _json
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        from sqlalchemy import select, func
        a_stmt = select(db.AnalysisRecord).where(
            db.AnalysisRecord.issue_id == issue_id
        ).order_by(db.AnalysisRecord.created_at.desc()).limit(1)
        analysis = (await session.execute(a_stmt)).scalar_one_or_none()

        a_count_stmt = select(func.count()).select_from(db.AnalysisRecord).where(db.AnalysisRecord.issue_id == issue_id)
        a_count = (await session.execute(a_count_stmt)).scalar() or 0

        t_stmt = select(db.TaskRecord).where(
            db.TaskRecord.issue_id == issue_id
        ).order_by(db.TaskRecord.created_at.desc()).limit(1)
        task = (await session.execute(t_stmt)).scalar_one_or_none()

        return db._issue_to_dict(issue, analysis=analysis, task=task, analysis_count=a_count)


@router.get("/{issue_id}/files/{filename:path}")
async def serve_issue_file(issue_id: str, filename: str):
    """Serve a file (image/log) from an issue's workspace."""
    settings = get_settings()

    # Look in multiple possible locations
    search_dirs = [
        Path(settings.storage.workspace_dir) / issue_id / "raw",
        Path(settings.storage.workspace_dir) / issue_id / "processed",
    ]

    # Also check task workspaces that reference this issue
    async with db.get_session() as session:
        from sqlalchemy import select
        stmt = select(db.TaskRecord).where(db.TaskRecord.issue_id == issue_id).order_by(db.TaskRecord.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        task = result.scalar_one_or_none()
        if task:
            search_dirs.insert(0, Path(settings.storage.workspace_dir) / task.id / "raw")
            search_dirs.insert(1, Path(settings.storage.workspace_dir) / task.id / "images")

    for d in search_dirs:
        file_path = d / filename
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)

    raise HTTPException(status_code=404, detail="File not found")


@router.delete("/{issue_id}")
async def delete_issue(issue_id: str):
    """Soft-delete an issue (mark as deleted, hide from UI)."""
    ok = await db.soft_delete_issue(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Issue not found")
    return {"status": "deleted", "issue_id": issue_id}


@router.post("/{issue_id}/inaccurate")
async def mark_inaccurate(issue_id: str):
    """Mark an issue's analysis as inaccurate."""
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

    await db.update_issue_status(issue_id, "inaccurate")
    await db.log_event("mark_inaccurate", issue_id=issue_id)
    return {"status": "ok"}
