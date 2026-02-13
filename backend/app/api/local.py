"""
API routes for locally-tracked issues (analyzed by Jarvis).

- 进行中: issues.status = 'analyzing'
- 已完成: issues.status = 'done'
- 失败:   issues.status = 'failed'
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from pydantic import BaseModel

from app.db import database as db
from app.services.notify import notify_oncall

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
    created_by: Optional[str] = Query(None, description="Filter by creator username"),
):
    """List ALL locally-tracked issues (for tracking page). Supports filtering by creator."""
    items, total = await db.get_tracked_issues_paginated(page, page_size, created_by=created_by)
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.delete("/{issue_id}")
async def delete_issue(issue_id: str):
    """Soft-delete an issue (mark as deleted, hide from UI)."""
    ok = await db.soft_delete_issue(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Issue not found")
    return {"status": "deleted", "issue_id": issue_id}


class EscalateRequest(BaseModel):
    reason: str = "用户手动转工程师"


@router.post("/{issue_id}/escalate")
async def escalate_to_engineer(issue_id: str, req: EscalateRequest):
    """Escalate an issue to oncall engineers (send Feishu notification)."""
    # Get issue info from DB
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        # Check if oncall is configured
        oncall_members = await db.get_current_oncall()
        if not oncall_members:
            return {"status": "no_oncall", "issue_id": issue_id, "message": "暂无值班人员，请先在值班管理中配置"}

        sent = await notify_oncall(
            issue_id=issue.id,
            description=issue.description or "",
            reason=req.reason,
            zendesk_id=issue.zendesk_id or "",
            link=issue.feishu_link or "",
        )

    if sent:
        return {"status": "sent", "issue_id": issue_id, "message": f"已通知 {', '.join(oncall_members)}"}
    else:
        return {"status": "send_failed", "issue_id": issue_id, "message": f"发送失败，请检查飞书邮箱是否正确: {', '.join(oncall_members)}"}
