"""
Analytics API: event tracking + dashboard data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.db import database as db

logger = logging.getLogger("jarvis.api.analytics")
router = APIRouter()


class TrackEventRequest(BaseModel):
    event_type: str       # page_visit, button_click, etc.
    issue_id: str = ""
    username: str = ""
    detail: dict = {}


@router.post("/track")
async def track_event(req: TrackEventRequest):
    """Track a frontend event (page visit, button click, etc.)."""
    await db.log_event(
        event_type=req.event_type,
        issue_id=req.issue_id,
        username=req.username,
        detail=req.detail,
    )
    return {"status": "ok"}


@router.get("/dashboard")
async def get_dashboard(
    days: int = Query(7, ge=1, le=90, description="Number of days to look back"),
):
    """Get analytics dashboard data."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    data = await db.get_analytics(date_from, date_to)

    # Calculate value metrics
    success = data["successful_analyses"]
    failed = data["failed_analyses"]
    completed = success + failed  # total finished analyses (denominator for success rate)
    total = max(data["total_analyses"], completed)  # use whichever is larger (start events may be missing for old data)
    avg_min = data["avg_analysis_duration_min"]

    manual_time_min = total * 30
    ai_time_min = total * avg_min if avg_min else total * 5
    time_saved_min = max(0, manual_time_min - ai_time_min)
    time_saved_hours = round(time_saved_min / 60, 1)

    data["value_metrics"] = {
        "time_saved_hours": time_saved_hours,
        "time_saved_per_ticket_min": round(30 - avg_min, 1) if avg_min else 25,
        "success_rate": round(success / completed * 100, 1) if completed else 0,
        "estimated_manual_hours": round(manual_time_min / 60, 1),
        "estimated_ai_hours": round(ai_time_min / 60, 1),
    }

    return data
