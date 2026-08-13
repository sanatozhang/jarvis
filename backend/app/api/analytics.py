"""
Analytics API: event tracking + dashboard data.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api._window import window_params
from app.db import database as db
from app.services.date_window import to_datetime_bounds

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
async def get_dashboard(window: tuple = Depends(window_params(7))):
    """Get analytics dashboard data."""
    date_from, date_to = window
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
async def get_problem_type_stats(window: tuple = Depends(window_params(30))):
    """Get problem type distribution, daily trend, and top 10."""
    date_from, date_to = window
    return await db.get_problem_type_stats(date_from, date_to)


@router.get("/classification-stats")
async def get_classification_stats(window: tuple = Depends(window_params(30))):
    """Get problem category + device type classification stats (pie chart data)."""
    date_from, date_to = window
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


@router.get("/fix-effectiveness")
async def get_fix_effectiveness(window: tuple = Depends(window_params(30))):
    """Did what got marked 'done' this period actually stay fixed?

    Reports two DIFFERENT recurrence rates — see db.get_fix_effectiveness's
    docstring for why they aren't interchangeable. `cohort_recurrence_rate`
    is the one the UI should lead with (answers "did our fixes hold");
    `recurrence_rate_by_detection` is the smaller-print operational counter
    (answers "how much recurrence fired this period").
    """
    date_from, date_to = window
    return await db.get_fix_effectiveness(date_from, date_to)


@router.get("/rule-accuracy")
async def get_rule_accuracy(window: tuple = Depends(window_params(30))):
    """Get rule accuracy statistics."""
    from app.services.rule_accuracy import get_rule_accuracy_stats
    date_from, date_to = window
    return await get_rule_accuracy_stats(date_from=date_from, date_to=date_to)


@router.get("/engineer-label-accuracy")
async def get_engineer_label_accuracy(window: tuple = Depends(window_params(30))):
    """T3: AI needs_engineer 标签准确性 — 基于客服反馈做混淆矩阵 + precision/recall。

    抓手：让"AI 标得准不准"从拍脑袋变成可量化数据。
    - TP: AI=True, 实际=True（漏不掉，研发该接的接住了）
    - FP: AI=True, 实际=False（误报，AI 把客服自助能搞定的也甩给研发）
    - FN: AI=False, 实际=True（漏报，研发该接的被 AI 放过了——最危险）
    - TN: AI=False, 实际=False（正确放行）
    """
    from sqlalchemy import select, func, and_

    date_from, date_to = window
    start, end = to_datetime_bounds(date_from, date_to)

    async with db.get_session() as session:
        # 只看有客服反馈的 analyses
        stmt = select(
            db.AnalysisRecord.needs_engineer,
            db.AnalysisRecord.engineer_label_feedback,
            func.count(),
        ).where(
            and_(
                db.AnalysisRecord.created_at >= start,
                db.AnalysisRecord.created_at <= end,
                db.AnalysisRecord.engineer_label_feedback.is_not(None),
            )
        ).group_by(
            db.AnalysisRecord.needs_engineer,
            db.AnalysisRecord.engineer_label_feedback,
        )
        rows = (await session.execute(stmt)).all()

    matrix = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for ai, actual, n in rows:
        if ai and actual:        matrix["tp"] += n
        elif ai and not actual:  matrix["fp"] += n
        elif not ai and actual:  matrix["fn"] += n
        else:                    matrix["tn"] += n

    total = sum(matrix.values())
    tp, fp, fn, tn = matrix["tp"], matrix["fp"], matrix["fn"], matrix["tn"]

    precision = round(tp / (tp + fp) * 100, 2) if (tp + fp) else None
    recall = round(tp / (tp + fn) * 100, 2) if (tp + fn) else None
    f1 = round(2 * precision * recall / (precision + recall), 2) if (precision and recall) else None
    accuracy = round((tp + tn) / total * 100, 2) if total else None

    return {
        "window_days": (end.date() - start.date()).days + 1,
        "date_from": date_from,
        "date_to": date_to,
        "labeled_total": total,
        "confusion_matrix": matrix,
        "precision_pct": precision,   # AI 说要工程师里，实际真的要的占比（高 = 不乱甩单）
        "recall_pct": recall,         # 真的要工程师里，AI 抓到的占比（高 = 不漏）
        "f1_pct": f1,
        "accuracy_pct": accuracy,
        "hint": "precision 低 = 误报多（骚扰研发）；recall 低 = 漏报多（客服漏接 → 用户投诉）",
    }


@router.get("/fallback-extraction")
async def get_fallback_extraction_rate(window: tuple = Depends(window_params(7))):
    """L4.2 监控指标：AI 没写 result.json 走 Markdown 兜底的占比。

    阈值参考：> 5% 说明 prompt/平台合约不够强，需要排查。
    """
    from sqlalchemy import func, select, and_

    date_from, date_to = window
    start, end = to_datetime_bounds(date_from, date_to)
    fallback_marker = "Agent 未生成 result.json，从 Markdown 输出中提取"

    async with db.get_session() as session:
        total_stmt = select(func.count()).select_from(db.AnalysisRecord).where(
            and_(db.AnalysisRecord.created_at >= start, db.AnalysisRecord.created_at <= end),
        )
        total = (await session.execute(total_stmt)).scalar() or 0

        fallback_stmt = select(func.count()).select_from(db.AnalysisRecord).where(
            and_(
                db.AnalysisRecord.created_at >= start,
                db.AnalysisRecord.created_at <= end,
                db.AnalysisRecord.confidence_reason == fallback_marker,
            )
        )
        fallback = (await session.execute(fallback_stmt)).scalar() or 0

        # 按 agent 分组
        agent_stmt = select(
            db.AnalysisRecord.agent_type,
            func.count(),
        ).where(
            and_(
                db.AnalysisRecord.created_at >= start,
                db.AnalysisRecord.created_at <= end,
                db.AnalysisRecord.confidence_reason == fallback_marker,
            )
        ).group_by(db.AnalysisRecord.agent_type)
        by_agent = {row[0] or "unknown": row[1] for row in (await session.execute(agent_stmt)).all()}

    rate = round(fallback / total * 100, 2) if total else 0.0
    return {
        "window_days": (end.date() - start.date()).days + 1,
        "date_from": date_from,
        "date_to": date_to,
        "total_analyses": total,
        "fallback_extractions": fallback,
        "fallback_rate_pct": rate,
        "threshold_pct": 5.0,
        "alert": rate > 5.0,
        "by_agent": by_agent,
        "marker": fallback_marker,
    }
