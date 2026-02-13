"""
API routes for oncall schedule management.

- Admin: create/edit oncall groups
- All users: view current oncall, schedule
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
