"""
API routes for analysis tasks: create, status, stream, batch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.db import database as db
from app.models.schemas import (
    AnalysisResult,
    BatchAnalyzeRequest,
    TaskCreate,
    TaskProgress,
    TaskStatus,
)
from app.workers.analysis_worker import run_analysis_pipeline

logger = logging.getLogger("jarvis.api.tasks")
router = APIRouter()

# In-memory progress store (for SSE streaming)
# In production, use Redis pub/sub instead.
_progress_store: dict[str, TaskProgress] = {}


def _update_progress(task_id: str, progress: TaskProgress):
    _progress_store[task_id] = progress


@router.post("", response_model=TaskProgress)
async def create_task(req: TaskCreate, background_tasks: BackgroundTasks):
    """Create a new analysis task for an issue."""
    task_id = f"task_{uuid.uuid4().hex[:12]}"

    agent_type_str = req.agent_type.value if req.agent_type else ""
    await db.create_task(task_id=task_id, issue_id=req.issue_id, agent_type=agent_type_str)

    # IMMEDIATELY mark issue as "analyzing" in local DB
    await db.update_issue_status(req.issue_id, "analyzing")
    if req.username:
        await db.set_issue_created_by(req.issue_id, req.username)

    # Track: analysis started
    await db.log_event("analysis_start", issue_id=req.issue_id, username=req.username)

    progress = TaskProgress(
        task_id=task_id,
        issue_id=req.issue_id,
        status=TaskStatus.QUEUED,
        progress=0,
        message="排队中...",
    )
    _update_progress(task_id, progress)

    # Launch analysis in background
    background_tasks.add_task(
        _run_task,
        task_id=task_id,
        issue_id=req.issue_id,
        agent_override=agent_type_str or None,
        username=req.username or "",
        followup_question=req.followup_question or "",
    )

    return progress


@router.post("/batch", response_model=list[TaskProgress])
async def batch_analyze(req: BatchAnalyzeRequest, background_tasks: BackgroundTasks):
    """Create analysis tasks for multiple issues."""
    results = []
    for issue_id in req.issue_ids:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        agent_type_str = req.agent_type.value if req.agent_type else ""
        await db.create_task(task_id=task_id, issue_id=issue_id, agent_type=agent_type_str)
        await db.log_event("analysis_start", issue_id=issue_id, username="batch")

        progress = TaskProgress(
            task_id=task_id,
            issue_id=issue_id,
            status=TaskStatus.QUEUED,
            progress=0,
            message="排队中...",
        )
        _update_progress(task_id, progress)

        background_tasks.add_task(
            _run_task,
            task_id=task_id,
            issue_id=issue_id,
            agent_override=agent_type_str or None,
            username="batch",
        )
        results.append(progress)

    return results


@router.get("/{task_id}", response_model=TaskProgress)
async def get_task_status(task_id: str):
    """Get current task status."""
    # Check in-memory first (more up-to-date)
    if task_id in _progress_store:
        return _progress_store[task_id]

    # Fall back to DB
    record = await db.get_task(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskProgress(
        task_id=record.id,
        issue_id=record.issue_id,
        status=TaskStatus(record.status),
        progress=record.progress,
        message=record.message,
        error=record.error,
    )


@router.get("/{task_id}/result", response_model=Optional[AnalysisResult])
async def get_task_result(task_id: str):
    """Get the analysis result for a completed task."""
    record = await db.get_task(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Task not found")

    analysis = await db.get_analysis_by_issue(record.issue_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found yet")

    return AnalysisResult(
        task_id=analysis.task_id,
        issue_id=analysis.issue_id,
        problem_type=analysis.problem_type,
        problem_categories=json.loads(analysis.problem_categories_json) if analysis.problem_categories_json else [],
        device_type=analysis.device_type or "",
        root_cause=analysis.root_cause,
        confidence=analysis.confidence,
        confidence_reason=analysis.confidence_reason,
        key_evidence=json.loads(analysis.key_evidence_json) if analysis.key_evidence_json else [],
        user_reply=analysis.user_reply,
        needs_engineer=analysis.needs_engineer,
        fix_suggestion=analysis.fix_suggestion,
        rule_type=analysis.rule_type,
        agent_type=analysis.agent_type,
        raw_output=analysis.raw_output[:2000],
        created_at=analysis.created_at,
    )


@router.get("/{task_id}/stream")
async def stream_task_progress(task_id: str):
    """SSE endpoint for real-time task progress updates."""

    async def event_generator():
        last_progress = -1
        idle_count = 0
        max_idle = 600  # 10 minutes without any change

        while idle_count < max_idle:
            # Always read from DB as primary source (survives server reloads)
            progress = None
            mem = _progress_store.get(task_id)
            record = await db.get_task(task_id)

            if record:
                progress = TaskProgress(
                    task_id=record.id,
                    issue_id=record.issue_id,
                    status=TaskStatus(record.status),
                    progress=record.progress,
                    message=record.message or "",
                    error=record.error,
                )

            # In-memory may be more up-to-date than DB
            if mem and (not progress or mem.progress >= progress.progress):
                progress = mem

            if progress:
                data = progress.model_dump_json()
                yield f"data: {data}\n\n"

                if progress.status in (TaskStatus.DONE, TaskStatus.FAILED):
                    break

                if progress.progress != last_progress:
                    last_progress = progress.progress
                    idle_count = 0
                else:
                    idle_count += 1
            else:
                idle_count += 1

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("", response_model=list[TaskProgress])
async def list_tasks(limit: int = Query(50, le=200)):
    """List recent tasks."""
    records = await db.list_tasks(limit=limit)
    return [
        TaskProgress(
            task_id=r.id,
            issue_id=r.issue_id,
            status=TaskStatus(r.status),
            progress=r.progress,
            message=r.message,
            error=r.error,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in records
    ]


@router.post("/fix-false-failures")
async def fix_false_failures():
    """One-time fix: re-evaluate all 'failed' tasks and correct those that
    actually have valid analysis content (misclassified by the old logic).

    Safe to call multiple times — only changes tasks that match the criteria.
    """
    from app.db.database import get_session, TaskRecord, AnalysisRecord, IssueRecord
    from sqlalchemy import select

    _system_failure_types = {
        "分析超时", "日志解析失败", "Agent 不可用",
        "OpenAI 额度不足", "Claude 额度不足", "所有模型额度不足",
    }

    async with get_session() as session:
        # Get all failed tasks with their analyses
        stmt = (
            select(TaskRecord, AnalysisRecord)
            .outerjoin(AnalysisRecord, AnalysisRecord.task_id == TaskRecord.id)
            .where(TaskRecord.status == "failed")
        )
        rows = (await session.execute(stmt)).all()

        fixed_tasks = []
        for task, analysis in rows:
            if not analysis:
                continue

            pt = analysis.problem_type or ""
            rc = (analysis.root_cause or "").strip()

            # Apply the new is_real_failure logic
            _error_markers = {"未产出结构化结果"}
            is_only_error = any(m in rc for m in _error_markers) and len(rc) < 100
            is_short_error = len(rc) < 120 and any(
                kw in rc.lower() for kw in ["max turns", "reached max", "error:"]
            )
            has_substance = bool(rc) and not is_only_error and not is_short_error
            has_real_type = bool(pt and pt not in _system_failure_types and pt != "未知")

            is_fail = pt in _system_failure_types or (pt == "未知" and not has_substance)
            if pt not in _system_failure_types and (has_substance or has_real_type):
                is_fail = False

            if not is_fail:
                # This task was wrongly marked as failed → fix it
                task.status = "done"
                task.message = "分析完成（已修正）"
                task.error = None

                # Also fix the issue status
                issue = await session.get(IssueRecord, task.issue_id)
                if issue and issue.status == "failed":
                    issue.status = "done"

                fixed_tasks.append({
                    "task_id": task.id,
                    "issue_id": task.issue_id,
                    "problem_type": pt,
                })

        await session.commit()

    logger.info("fix-false-failures: corrected %d/%d failed tasks", len(fixed_tasks), len(rows))
    return {"total_failed": len(rows), "fixed": len(fixed_tasks), "details": fixed_tasks}


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------
async def _run_task(task_id: str, issue_id: str, agent_override: Optional[str] = None, username: str = "", followup_question: str = ""):
    """Run the full analysis pipeline as a background task."""
    import time as _time
    _start_time = _time.monotonic()
    try:
        async def on_progress(pct: int, msg: str):
            status = TaskStatus.ANALYZING
            if pct <= 20:
                status = TaskStatus.DOWNLOADING
            elif pct <= 35:
                status = TaskStatus.DECRYPTING
            elif pct <= 55:
                status = TaskStatus.EXTRACTING
            elif pct < 100:
                status = TaskStatus.ANALYZING

            progress = TaskProgress(
                task_id=task_id,
                issue_id=issue_id,
                status=status,
                progress=pct,
                message=msg,
                updated_at=datetime.utcnow(),
            )
            _update_progress(task_id, progress)
            await db.update_task(task_id, status=status.value, progress=pct, message=msg)

        result = await run_analysis_pipeline(
            issue_id=issue_id,
            task_id=task_id,
            agent_override=agent_override,
            on_progress=on_progress,
            followup_question=followup_question,
        )

        # Determine if this is a system-level failure vs a completed analysis.
        #
        # Guiding principle: if the agent produced ANY analytical content
        # (root_cause with real text, or a non-default problem_type),
        # treat it as success — even if result.json wasn't written properly.
        # Only infrastructure errors (timeout, quota, crash) are real failures.

        _system_failure_types = {
            "分析超时", "日志解析失败", "Agent 不可用",
            "OpenAI 额度不足", "Claude 额度不足", "所有模型额度不足",
        }

        # Check if root_cause has real analytical content (not just error boilerplate)
        rc = (result.root_cause or "").strip()
        _error_markers = {"未产出结构化结果"}
        # root_cause contains only error boilerplate (no real analysis)
        is_only_error = any(m in rc for m in _error_markers) and len(rc) < 100
        # Short error-like outputs (< 120 chars) with only error keywords and no analysis
        is_short_error = len(rc) < 120 and any(kw in rc.lower() for kw in ["max turns", "reached max", "error:"])
        has_substance = bool(rc) and not is_only_error and not is_short_error

        # Check if problem_type is a real classification (not a default/system value)
        has_real_type = bool(
            result.problem_type
            and result.problem_type not in _system_failure_types
            and result.problem_type != "未知"
        )

        is_real_failure = (
            # Case 1: known system error type (always fail, no override)
            result.problem_type in _system_failure_types
            # Case 2: problem_type is "未知" AND no real analysis content
            or (result.problem_type == "未知" and not has_substance)
        )

        # Override: if problem_type is NOT a system error, and we have
        # substance or a real type, it's a successful analysis
        if result.problem_type not in _system_failure_types:
            if has_substance or has_real_type:
                is_real_failure = False

        await db.save_analysis(result.model_dump())

        if is_real_failure:
            error_msg = result.root_cause[:200]
            await db.update_task(task_id, status="failed", progress=100, message="分析失败", error=error_msg)
            await db.update_issue_status(issue_id, "failed")
            _update_progress(task_id, TaskProgress(
                task_id=task_id, issue_id=issue_id, status=TaskStatus.FAILED,
                progress=100, message="分析失败", error=error_msg, updated_at=datetime.utcnow(),
            ))
            # Auto-notify oncall engineers on analysis failure
            try:
                from app.services.notify import notify_oncall
                await notify_oncall(
                    issue_id=issue_id,
                    description=result.root_cause[:200],
                    reason=f"AI 分析失败: {result.problem_type}",
                    zendesk_id="",
                )
            except Exception as ne:
                logger.warning("Failed to notify oncall on analysis failure: %s", ne)

            # Track: analysis failed
            duration = int((_time.monotonic() - _start_time) * 1000)
            await db.log_event("analysis_fail", issue_id=issue_id, username=username, duration_ms=duration, detail={"reason": result.problem_type, "error": error_msg[:200]})
        else:
            await db.update_task(task_id, status="done", progress=100, message="分析完成")
            await db.update_issue_status(issue_id, "done")
            _update_progress(task_id, TaskProgress(
                task_id=task_id, issue_id=issue_id, status=TaskStatus.DONE,
                progress=100, message="分析完成", updated_at=datetime.utcnow(),
            ))

            # Track: analysis succeeded
            duration = int((_time.monotonic() - _start_time) * 1000)
            await db.log_event("analysis_done", issue_id=issue_id, username=username, duration_ms=duration, detail={"rule_type": result.rule_type, "confidence": str(result.confidence)})

    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e, exc_info=True)
        error_str = str(e)[:500]

        # Always update in-memory progress first (this never fails)
        _update_progress(
            task_id,
            TaskProgress(
                task_id=task_id,
                issue_id=issue_id,
                status=TaskStatus.FAILED,
                progress=0,
                message="分析失败",
                error=error_str,
                updated_at=datetime.utcnow(),
            ),
        )

        # Try to persist failure to DB; if DB is unavailable (e.g. disk full),
        # log the error — the zombie cleanup task will mark it failed later.
        try:
            await db.update_task(task_id, status="failed", error=error_str)
            await db.update_issue_status(issue_id, "failed")
            duration = int((_time.monotonic() - _start_time) * 1000)
            await db.log_event("analysis_fail", issue_id=issue_id, username=username, duration_ms=duration, detail={"reason": "exception", "error": error_str[:200]})
        except Exception as db_err:
            logger.error("Failed to persist task failure to DB (disk full?): %s", db_err)
