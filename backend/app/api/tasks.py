"""
API routes for analysis tasks: create, status, stream, batch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.db import database as db
from app.models.schemas import (
    AnalysisResult,
    BatchAnalyzeRequest,
    Issue,
    LogFile,
    TaskCreate,
    TaskProgress,
    TaskSource,
    TaskStatus,
)
from app.workers.analysis_worker import run_analysis_pipeline

logger = logging.getLogger("jarvis.api.tasks")
router = APIRouter()

# In-memory progress store (for SSE streaming)
# In production, use Redis pub/sub instead.
_progress_store: dict[str, TaskProgress] = {}
_settings = get_settings()
_task_semaphore = asyncio.Semaphore(max(1, _settings.concurrency.max_agent_sessions))


def _update_progress(task_id: str, progress: TaskProgress):
    _progress_store[task_id] = progress


@router.post("", response_model=TaskProgress)
async def create_task(req: TaskCreate, background_tasks: BackgroundTasks):
    """Create a new analysis task for an issue."""
    if req.source == TaskSource.USER_UPLOAD:
        raise HTTPException(status_code=400, detail="user_upload 任务请使用 /api/tasks/feedback")

    task_id = f"task_{uuid.uuid4().hex[:12]}"

    agent_type_str = req.agent_type.value if req.agent_type else ""
    await db.create_task(
        task_id=task_id,
        issue_id=req.issue_id,
        agent_type=agent_type_str,
        source=req.source.value,
    )

    # IMMEDIATELY mark issue as "analyzing" in local DB
    # (so it shows in the in-progress tab right away, before the background task starts)
    await db.update_issue_status(req.issue_id, "analyzing")

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


@router.post("/feedback", response_model=TaskProgress)
async def create_feedback_task(
    background_tasks: BackgroundTasks,
    description: str = Form(...),
    device_sn: str = Form(""),
    firmware: str = Form(""),
    app_version: str = Form(""),
    zendesk: str = Form(""),
    agent_type: str = Form(""),
    files: list[UploadFile] = File(...),
):
    """Create analysis task from user-uploaded logs."""
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个日志文件")
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="单次最多上传 10 个文件")
    agent_type_str = (agent_type or "").strip().lower()
    if agent_type_str not in ("", "codex", "claude_code"):
        raise HTTPException(status_code=400, detail="agent_type 仅支持 codex 或 claude_code")

    issue_id = f"usr_{uuid.uuid4().hex[:10]}"
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    workspace = Path(_settings.storage.workspace_dir) / task_id
    raw_dir = workspace / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []
    log_files: list[LogFile] = []

    for idx, up in enumerate(files):
        safe_name = _safe_filename(up.filename or f"upload_{idx}.bin")
        save_path = raw_dir / safe_name
        if save_path.exists():
            save_path = raw_dir / f"{save_path.stem}_{idx}{save_path.suffix}"

        async with aiofiles.open(save_path, "wb") as out:
            while True:
                chunk = await up.read(1024 * 1024)
                if not chunk:
                    break
                await out.write(chunk)
        await up.close()

        size = save_path.stat().st_size
        saved_files.append(save_path)
        log_files.append(LogFile(name=save_path.name, token="", size=size))

    issue = Issue(
        record_id=issue_id,
        description=description[:1000],
        device_sn=device_sn,
        firmware=firmware,
        app_version=app_version,
        priority="",
        zendesk=zendesk,
        log_files=log_files,
    )

    await db.upsert_issue({**issue.model_dump(), "source": TaskSource.USER_UPLOAD.value}, status="analyzing")

    await db.create_task(
        task_id=task_id,
        issue_id=issue_id,
        agent_type=agent_type_str,
        source=TaskSource.USER_UPLOAD.value,
        workspace_path=str(workspace),
    )

    progress = TaskProgress(
        task_id=task_id,
        issue_id=issue_id,
        status=TaskStatus.QUEUED,
        progress=0,
        message="已上传，排队分析中...",
    )
    _update_progress(task_id, progress)

    background_tasks.add_task(
        _run_task,
        task_id=task_id,
        issue_id=issue_id,
        agent_override=agent_type_str or None,
        issue_override=issue,
        local_files=saved_files,
    )
    return progress


@router.post("/batch", response_model=list[TaskProgress])
async def batch_analyze(req: BatchAnalyzeRequest, background_tasks: BackgroundTasks):
    """Create analysis tasks for multiple issues."""
    results = []
    for issue_id in req.issue_ids:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        agent_type_str = req.agent_type.value if req.agent_type else ""
        await db.create_task(
            task_id=task_id,
            issue_id=issue_id,
            agent_type=agent_type_str,
            source=TaskSource.FEISHU.value,
        )
        await db.update_issue_status(issue_id, "analyzing")

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

    analysis = await db.get_analysis_by_task(task_id)
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
        core_logs=json.loads(analysis.core_logs_json) if analysis.core_logs_json else [],
        code_locations=json.loads(analysis.code_locations_json) if analysis.code_locations_json else [],
        user_reply=analysis.user_reply,
        needs_engineer=analysis.needs_engineer,
        requires_more_info=analysis.requires_more_info,
        more_info_guidance=analysis.more_info_guidance,
        next_steps=json.loads(analysis.next_steps_json) if analysis.next_steps_json else [],
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
def _safe_filename(name: str) -> str:
    normalized = name.strip().replace("\\", "_").replace("/", "_")
    normalized = re.sub("[^0-9A-Za-z._\\-\\u4e00-\\u9fff]", "_", normalized)
    if not normalized:
        normalized = f"upload_{uuid.uuid4().hex[:6]}.bin"
    return normalized[:180]


async def _run_task(
    task_id: str,
    issue_id: str,
    agent_override: Optional[str] = None,
    issue_override: Optional[Issue] = None,
    local_files: Optional[list[Path]] = None,
):
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

        async with _task_semaphore:
            result = await run_analysis_pipeline(
                issue_id=issue_id,
                task_id=task_id,
                agent_override=agent_override,
                on_progress=on_progress,
                issue_override=issue_override,
                local_files=local_files,
            )

        # Check if the result is a real success or a disguised failure
        is_real_failure = (
            result.problem_type in ("分析超时", "日志解析失败", "Agent 不可用")
            or result.confidence == "low" and result.needs_engineer and not result.user_reply
        )

        await db.save_analysis(result.model_dump(mode="json"))

        if is_real_failure:
            error_msg = result.root_cause[:200]
            await db.update_task(task_id, status="failed", progress=100, message="分析失败", error=error_msg)
            await db.update_issue_status(issue_id, "failed")
            _update_progress(task_id, TaskProgress(
                task_id=task_id, issue_id=issue_id, status=TaskStatus.FAILED,
                progress=100, message="分析失败", error=error_msg, updated_at=datetime.utcnow(),
            ))
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
