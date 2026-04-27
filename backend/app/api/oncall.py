"""
API routes for oncall schedule management.

- Admin: create/edit oncall groups
- All users: view current oncall, schedule
- Escalated tickets: view/resolve with Feishu group notification
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import database as db

logger = logging.getLogger("jarvis.api.oncall")
router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class OncallGroupInput(BaseModel):
    members: List[str]  # feishu emails


class OncallScheduleInput(BaseModel):
    groups: List[OncallGroupInput]
    start_date: str  # ISO date: "2026-02-10"


# ---------------------------------------------------------------------------
# Read endpoints (all users)
# ---------------------------------------------------------------------------
@router.get("/current")
async def get_current_oncall():
    """Get this week's oncall members."""
    members = await db.get_current_oncall()
    return {"members": members, "count": len(members)}


@router.get("/schedule")
async def get_schedule():
    """Get full oncall schedule (all groups + config)."""
    groups = await db.get_oncall_groups()
    start_date = await db.get_oncall_config("start_date", "")
    return {
        "groups": groups,
        "start_date": start_date,
        "total_groups": len(groups),
    }


# ---------------------------------------------------------------------------
# Write endpoints (admin only — enforced by frontend, checked by username)
# ---------------------------------------------------------------------------
@router.put("/schedule")
async def update_schedule(
    req: OncallScheduleInput,
    username: str = Query(..., description="Admin username"),
):
    """Update oncall schedule (admin only)."""
    user = await db.get_user(username)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can edit oncall schedule")

    groups = [g.members for g in req.groups]
    await db.save_oncall_groups(groups, created_by=username)
    await db.set_oncall_config("start_date", req.start_date)

    logger.info("Oncall schedule updated by %s: %d groups, start=%s", username, len(groups), req.start_date)
    return {"status": "ok", "groups": len(groups), "start_date": req.start_date}


# ---------------------------------------------------------------------------
# Escalated tickets (oncall workload view)
# ---------------------------------------------------------------------------
@router.get("/tickets")
async def get_escalated_tickets(
    status: Optional[str] = Query(None, description="Filter: in_progress / resolved"),
    weeks: int = Query(2, description="Show tickets from the last N weeks (default 2 = current + previous)"),
):
    """Get escalated tickets scoped to recent oncall weeks."""
    from datetime import date, timedelta

    # Calculate the week boundary based on oncall start_date
    start_date_str = await db.get_oncall_config("start_date", "")
    if start_date_str:
        try:
            oncall_start = date.fromisoformat(start_date_str)
        except ValueError:
            oncall_start = None
    else:
        oncall_start = None

    # Determine the cutoff: start of (current_week - weeks + 1)
    today = date.today()
    if oncall_start:
        days_since_start = (today - oncall_start).days
        current_week_num = days_since_start // 7
        cutoff_week_num = max(0, current_week_num - weeks + 1)
        since_date = oncall_start + timedelta(weeks=cutoff_week_num)
    else:
        # Fallback: last N*7 days
        since_date = today - timedelta(days=weeks * 7)

    items = await db.get_escalated_issues(status=status, since_date=since_date)

    return {
        "tickets": items,
        "count": len(items),
        "since_date": since_date.isoformat(),
        "weeks": weeks,
    }


@router.put("/tickets/{issue_id}/resolve")
async def resolve_ticket(issue_id: str):
    """Mark an escalated ticket as resolved + notify Feishu group."""
    # Get issue info before resolving (need chat_id and description)
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
    if not issue or not issue.escalated_at:
        raise HTTPException(status_code=404, detail="Escalated issue not found")

    chat_id = issue.escalation_chat_id or ""
    description = issue.description or issue_id

    # Get problem type for the message
    problem_type = ""
    analysis = await db.get_analysis_by_issue(issue_id)
    if analysis:
        problem_type = analysis.problem_type or ""

    # Update DB status
    ok = await db.resolve_escalation(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Failed to resolve")

    # Send notification to Feishu group
    feishu_notified = False
    if chat_id:
        try:
            from app.services.feishu_cli import send_message
            msg = f"✅ 工单已标记完成\n问题: {description[:200]}"
            if problem_type:
                msg += f"\n分类: {problem_type}"
            await send_message(chat_id=chat_id, text=msg)
            feishu_notified = True
            logger.info("Sent resolve notification to group %s for issue %s", chat_id, issue_id)
        except Exception as e:
            logger.warning("Failed to send resolve notification to group: %s", e)

    return {"status": "resolved", "issue_id": issue_id, "feishu_notified": feishu_notified}
