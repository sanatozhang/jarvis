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


@router.delete("/{issue_id}")
async def delete_issue(issue_id: str):
    """Soft-delete an issue (mark as deleted, hide from UI)."""
    ok = await db.soft_delete_issue(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Issue not found")
    return {"status": "deleted", "issue_id": issue_id}


class EscalateRequest(BaseModel):
    reason: str = "用户手动转工程师"
    user_email: str = ""


@router.post("/{issue_id}/escalate")
async def escalate_to_engineer(issue_id: str, req: EscalateRequest):
    """
    Escalate an issue to oncall engineers.

    Creates a Feishu group chat with the current user and oncall members,
    then sends the issue link to the group.
    Group name: 工单处理--{problem_type}--{timestamp}
    """
    from app.services.notify import create_escalation_group, notify_oncall

    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        # Check if oncall is configured
        oncall_members = await db.get_current_oncall()
        if not oncall_members:
            return {"status": "no_oncall", "issue_id": issue_id, "message": "暂无值班人员，请先在值班管理中配置"}

        # Get the analysis result for problem_type
        analysis = await db.get_analysis_by_issue(issue_id)
        problem_type = analysis.problem_type if analysis else ""

        # Build issue link
        issue_link = ""
        if issue.linear_issue_url:
            issue_link = issue.linear_issue_url
        elif issue.feishu_link:
            issue_link = issue.feishu_link

        # Try to create a Feishu group chat
        user_email = req.user_email
        if user_email:
            try:
                result = await create_escalation_group(
                    user_email=user_email,
                    issue_id=issue.id,
                    description=issue.description or "",
                    problem_type=problem_type,
                    issue_link=issue_link,
                    zendesk_id=issue.zendesk_id or "",
                )

                await db.log_event("escalate", issue_id=issue_id, detail={
                    "reason": req.reason,
                    "group_name": result["group_name"],
                    "chat_id": result["chat_id"],
                    "members": result["members"],
                })

                return {
                    "status": "sent",
                    "issue_id": issue_id,
                    "message": f"已创建飞书群: {result['group_name']}",
                    "group_name": result["group_name"],
                    "chat_id": result["chat_id"],
                }
            except Exception as e:
                logger.error("Failed to create escalation group: %s", e)
                # Fallback to simple notification
                pass

        # Fallback: send direct notification (no group chat)
        sent = await notify_oncall(
            issue_id=issue.id,
            description=issue.description or "",
            reason=req.reason,
            zendesk_id=issue.zendesk_id or "",
            link=issue_link,
        )

        await db.log_event("escalate", issue_id=issue_id, detail={"reason": req.reason, "sent": sent})

        if sent:
            return {"status": "sent", "issue_id": issue_id, "message": f"已通知 {', '.join(oncall_members)}"}
        else:
            return {"status": "send_failed", "issue_id": issue_id, "message": f"发送失败，请检查飞书邮箱是否正确: {', '.join(oncall_members)}"}
