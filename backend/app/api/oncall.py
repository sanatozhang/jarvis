"""
API routes for oncall schedule management.

- Admin: create/edit oncall groups
- All users: view current oncall, schedule
- Escalated tickets: view/resolve with Feishu group notification
- Stats: per-week oncall workload statistics
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Optional

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
    weeks: int = Query(0, description="0 = all history, N = last N weeks"),
):
    """Get escalated tickets. weeks=0 returns all."""

    since_date = None
    if weeks > 0:
        start_date_str = await db.get_oncall_config("start_date", "")
        oncall_start = None
        if start_date_str:
            try:
                oncall_start = date.fromisoformat(start_date_str)
            except ValueError:
                pass

        today = date.today()
        if oncall_start:
            days_since_start = (today - oncall_start).days
            current_week_num = days_since_start // 7
            cutoff_week_num = max(0, current_week_num - weeks + 1)
            since_date = oncall_start + timedelta(weeks=cutoff_week_num)
        else:
            since_date = today - timedelta(days=weeks * 7)

    items = await db.get_escalated_issues(status=status, since_date=since_date)

    return {
        "tickets": items,
        "count": len(items),
        "since_date": since_date.isoformat() if since_date else "",
        "weeks": weeks,
    }


@router.get("/stats")
async def get_oncall_stats():
    """Per-week oncall workload statistics."""
    groups = await db.get_oncall_groups()
    start_date_str = await db.get_oncall_config("start_date", "")
    if not start_date_str or not groups:
        return {"weeks": [], "groups": [g["members"] for g in groups]}

    oncall_start = date.fromisoformat(start_date_str)
    today = date.today()
    total_groups = len(groups)
    current_week_num = max(0, (today - oncall_start).days // 7)

    # Fetch ALL escalated tickets
    all_tickets = await db.get_escalated_issues()

    # Build a lookup: week_number -> list of tickets
    week_tickets: Dict[int, List[Dict[str, Any]]] = {}
    for tk in all_tickets:
        esc_at = tk.get("escalated_at", "")
        if not esc_at:
            continue
        esc_date = date.fromisoformat(esc_at[:10])
        wn = (esc_date - oncall_start).days // 7
        week_tickets.setdefault(wn, []).append(tk)

    # Build week stats (most recent first, up to 12 weeks)
    week_stats = []
    start_week = max(0, current_week_num - 11)
    for wn in range(current_week_num, start_week - 1, -1):
        gi = wn % total_groups
        w_start = oncall_start + timedelta(weeks=wn)
        w_end = w_start + timedelta(days=6)
        tks = week_tickets.get(wn, [])
        in_progress = sum(1 for t in tks if t.get("escalation_status") != "resolved")
        resolved = sum(1 for t in tks if t.get("escalation_status") == "resolved")
        week_stats.append({
            "week_num": wn,
            "group_index": gi,
            "members": groups[gi]["members"],
            "week_start": w_start.isoformat(),
            "week_end": w_end.isoformat(),
            "is_current": wn == current_week_num,
            "total": len(tks),
            "in_progress": in_progress,
            "resolved": resolved,
        })

    return {
        "weeks": week_stats,
        "groups": [g["members"] for g in groups],
        "start_date": start_date_str,
        "current_week_num": current_week_num,
    }


@router.put("/tickets/{issue_id}/resolve")
async def resolve_ticket(issue_id: str):
    """Mark an escalated ticket as resolved + notify Feishu group."""
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
    if not issue or not issue.escalated_at:
        raise HTTPException(status_code=404, detail="Escalated issue not found")

    chat_id = issue.escalation_chat_id or ""
    description = issue.description or issue_id

    problem_type = ""
    analysis = await db.get_analysis_by_issue(issue_id)
    if analysis:
        problem_type = analysis.problem_type or ""

    ok = await db.resolve_escalation(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Failed to resolve")

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
