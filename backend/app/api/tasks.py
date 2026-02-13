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


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------
async def _run_task(task_id: str, issue_id: str, agent_override: Optional[str] = None):
    """Run the full analysis pipeline as a background task."""
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
        )

        # Check if the result is a real success or a disguised failure
        is_real_failure = (
            result.problem_type in ("分析超时", "日志解析失败", "Agent 不可用")
            or result.confidence == "low" and result.needs_engineer and not result.user_reply
        )

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
        else:
            await db.update_task(task_id, status="done", progress=100, message="分析完成")
            await db.update_issue_status(issue_id, "done")
            _update_progress(task_id, TaskProgress(
                task_id=task_id, issue_id=issue_id, status=TaskStatus.DONE,
                progress=100, message="分析完成", updated_at=datetime.utcnow(),
            ))

    except Exception as e:
        logger.error("Task %s failed: %s", task_id, e, exc_info=True)
        await db.update_task(task_id, status="failed", error=str(e))
        await db.update_issue_status(issue_id, "failed")
        _update_progress(
            task_id,
            TaskProgress(
                task_id=task_id,
                issue_id=issue_id,
                status=TaskStatus.FAILED,
                progress=0,
                message="分析失败",
                error=str(e),
                updated_at=datetime.utcnow(),
            ),
        )
