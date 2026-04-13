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
    days: int = Query(7, ge=1, le=3650, description="Number of days to look back"),
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


@router.get("/problem-types")
async def get_problem_type_stats(
    days: int = Query(30, ge=1, le=3650, description="Number of days to look back"),
):
    """Get problem type distribution, daily trend, and top 10."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return await db.get_problem_type_stats(date_from, date_to)


@router.get("/classification-stats")
async def get_classification_stats(
    days: int = Query(30, ge=1, le=3650, description="Number of days to look back"),
):
    """Get problem category + device type classification stats (pie chart data)."""
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    date_from = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return await db.get_classification_stats(date_from, date_to)


@router.post("/backfill-classifications")
async def backfill_classifications(
    limit: int = Query(500, ge=1, le=5000, description="Max records to process"),
):
    """Backfill problem_categories for old analyses using keyword mapping."""
    records = await db.get_analyses_for_backfill(limit=limit)
    if not records:
        return {"status": "ok", "updated": 0, "message": "No records need backfill"}

    from app.classification_taxonomy import classify_problem

    updated = 0
    for rec in records:
        categories = classify_problem(rec["problem_type"], rec.get("root_cause", ""))
        device_type = rec.get("device_type", "") or ""
        if categories:
            await db.update_analysis_classification(rec["id"], categories, device_type)
            updated += 1

    return {"status": "ok", "updated": updated, "total_candidates": len(records)}


@router.get("/rule-accuracy")
async def get_rule_accuracy(
    days: int = Query(30, ge=1, le=3650, description="Number of days to look back"),
):
    """Get rule accuracy statistics."""
    from app.services.rule_accuracy import get_rule_accuracy_stats
    return await get_rule_accuracy_stats(days=days)
