"""
Public API v1: AI Analysis Service

Standalone API for external systems to submit analysis tasks.

Flow:
  1. POST /api/v1/analyze     → submit task, returns task_id immediately
  2. GET  /api/v1/analyze/:id → poll for result
  3. (optional) webhook callback when done

Supports:
  - Text description + log file upload
  - Async processing (no HTTP timeout issues)
  - Webhook callback to caller's URL
  - API key authentication
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import get_settings
from app.db import database as db
from app.services.decrypt import process_log_file
from app.services.extractor import extract_for_rules
from app.services.rule_engine import RuleEngine
from app.services.agent_orchestrator import AgentOrchestrator
from app.models.schemas import AnalysisResult, Issue

logger = logging.getLogger("jarvis.api.v1")
router = APIRouter()

# Simple API key auth (set in .env as JARVIS_API_KEY)
import os
API_KEY = os.environ.get("JARVIS_API_KEY", "")


def _check_api_key(authorization: Optional[str]):
    """Validate API key if configured."""
    if not API_KEY:
        return  # no key configured = open access
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header. Use: Bearer <api_key>")
    token = authorization.replace("Bearer ", "").strip()
    if token != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class AnalyzeResponse(BaseModel):
    task_id: str
    status: str  # processing / done / failed
    message: str = ""


class AnalyzeResult(BaseModel):
    task_id: str
    status: str
    problem_type: str = ""
    root_cause: str = ""
    confidence: str = ""
    key_evidence: list = []
    user_reply: str = ""
    needs_engineer: bool = False
    rule_type: str = ""
    agent_type: str = ""
    created_at: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Submit analysis
# ---------------------------------------------------------------------------
@router.post("/analyze", response_model=AnalyzeResponse)
async def submit_analysis(
    background_tasks: BackgroundTasks,
    description: str = Form(..., description="问题描述"),
    device_sn: str = Form("", description="设备 SN"),
    priority: str = Form("L", description="优先级 H/L"),
    webhook_url: str = Form("", description="完成后回调 URL（可选）"),
    log_files: list[UploadFile] = File(default=[], description="日志文件"),
    authorization: Optional[str] = Header(None),
):
    """
    Submit an analysis task. Returns immediately with a task_id.

    The AI analysis runs in the background. Use GET /api/v1/analyze/{task_id}
    to poll for the result, or provide a webhook_url for push notification.
    """
    _check_api_key(authorization)

    settings = get_settings()
    task_id = f"api_{uuid.uuid4().hex[:12]}"
    record_id = f"api_{uuid.uuid4().hex[:10]}"

    # Save uploaded files
    workspace = Path(settings.storage.workspace_dir) / task_id
    raw_dir = workspace / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    for f in log_files:
        if f.filename and f.size and f.size > 0:
            content = await f.read()
            dest = raw_dir / f.filename
            dest.write_bytes(content)
            saved_files.append({"name": f.filename, "size": len(content), "local_path": str(dest)})

    # Save issue to DB
    issue_data = {
        "record_id": record_id,
        "description": description,
        "device_sn": device_sn,
        "priority": priority,
        "created_at_ms": int(datetime.utcnow().timestamp() * 1000),
        "log_files": saved_files,
    }
    await db.upsert_issue(issue_data, status="analyzing")
    await db.create_task(task_id=task_id, issue_id=record_id)

    # Start background analysis
    background_tasks.add_task(
        _run_api_analysis,
        task_id=task_id,
        record_id=record_id,
        description=description,
        device_sn=device_sn,
        priority=priority,
        saved_files=saved_files,
        workspace=workspace,
        webhook_url=webhook_url or "",
    )

    logger.info("API analysis submitted: %s (files: %d, webhook: %s)", task_id, len(saved_files), bool(webhook_url))

    return AnalyzeResponse(
        task_id=task_id,
        status="processing",
        message=f"Analysis started. Poll GET /api/v1/analyze/{task_id} for result.",
    )


# ---------------------------------------------------------------------------
# Poll result
# ---------------------------------------------------------------------------
@router.get("/analyze/{task_id}", response_model=AnalyzeResult)
async def get_analysis_result(
    task_id: str,
    authorization: Optional[str] = Header(None),
):
    """
    Get the result of an analysis task.

    Returns status: "processing" | "done" | "failed"
    When done, includes the full analysis result.
    """
    _check_api_key(authorization)

    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    result = AnalyzeResult(
        task_id=task_id,
        status=task.status,
        error=task.error or "",
    )

    if task.status in ("done", "failed"):
        analysis = await db.get_analysis_by_issue(task.issue_id)
        if analysis:
            import json
            result.problem_type = analysis.problem_type or ""
            result.root_cause = analysis.root_cause or ""
            result.confidence = analysis.confidence or ""
            result.key_evidence = json.loads(analysis.key_evidence_json) if analysis.key_evidence_json else []
            result.user_reply = analysis.user_reply or ""
            result.needs_engineer = analysis.needs_engineer
            result.rule_type = analysis.rule_type or ""
            result.agent_type = analysis.agent_type or ""
            result.created_at = analysis.created_at.isoformat() if analysis.created_at else ""

    return result


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
async def _run_api_analysis(
    task_id: str,
    record_id: str,
    description: str,
    device_sn: str,
    priority: str,
    saved_files: list,
    workspace: Path,
    webhook_url: str,
):
    """Run the full analysis pipeline for an API-submitted task."""
    try:
        await db.update_task(task_id, status="analyzing", progress=10, message="处理日志文件...")

        # Process log files
        log_paths = []
        raw_dir = workspace / "raw"
        processed_dir = workspace / "processed"
        processed_dir.mkdir(exist_ok=True)

        for sf in saved_files:
            fp = Path(sf["local_path"])
            if fp.exists():
                log_path, incorrect, reason = process_log_file(fp, processed_dir)
                if log_path:
                    log_paths.append(log_path)

        if not log_paths:
            result = AnalysisResult(
                task_id=task_id, issue_id=record_id,
                problem_type="日志解析失败",
                root_cause="无可用日志文件" + (f" ({saved_files[0]['name']})" if saved_files else ""),
                confidence="low", needs_engineer=True,
                user_reply="日志文件无法解析，请检查文件格式。",
            )
            await _finish_task(task_id, record_id, result, webhook_url, is_failure=True)
            return

        await db.update_task(task_id, status="analyzing", progress=30, message="匹配规则...")

        # Match rules
        engine = RuleEngine()
        # Need to load from DB if available
        try:
            await engine.sync_files_to_db()
        except Exception:
            pass

        issue = Issue(
            record_id=record_id, description=description,
            device_sn=device_sn, priority=priority,
        )

        rules = engine.match_rules(description)
        rule_type = engine.classify(description)

        await db.update_task(task_id, status="analyzing", progress=50, message="AI 分析中...")

        # Pre-extract
        extraction = extract_for_rules(rules, log_paths)

        # Prepare workspace
        engine.prepare_workspace(workspace, rules, log_paths)

        # Run agent
        orchestrator = AgentOrchestrator()
        from app.agents.base import BaseAgent
        prompt = BaseAgent.build_prompt(issue=issue, rules=rules, extraction=extraction)

        agent = orchestrator.select_agent(rule_type)
        result = await agent.analyze(workspace=workspace, prompt=prompt)
        result.task_id = task_id
        result.issue_id = record_id
        result.rule_type = rule_type

        # Check if real failure
        is_failure = (
            result.problem_type in ("分析超时", "日志解析失败", "Agent 不可用", "未知")
            or (result.confidence == "low" and result.needs_engineer and not result.user_reply)
            or "未产出结构化结果" in (result.root_cause or "")
        )

        await _finish_task(task_id, record_id, result, webhook_url, is_failure=is_failure)

    except Exception as e:
        logger.error("API analysis %s failed: %s", task_id, e, exc_info=True)
        result = AnalysisResult(
            task_id=task_id, issue_id=record_id,
            problem_type="分析异常", root_cause=str(e),
            confidence="low", needs_engineer=True,
        )
        await _finish_task(task_id, record_id, result, webhook_url, is_failure=True)


async def _finish_task(
    task_id: str,
    record_id: str,
    result: AnalysisResult,
    webhook_url: str,
    is_failure: bool,
):
    """Save result to DB and optionally call webhook."""
    await db.save_analysis(result.model_dump())

    if is_failure:
        await db.update_task(task_id, status="failed", progress=100, message="分析失败", error=result.root_cause[:200])
        await db.update_issue_status(record_id, "failed")
    else:
        await db.update_task(task_id, status="done", progress=100, message="分析完成")
        await db.update_issue_status(record_id, "done")

    logger.info("API analysis %s finished: %s (failure=%s)", task_id, result.problem_type, is_failure)

    # Webhook callback
    if webhook_url:
        try:
            import json
            payload = {
                "task_id": task_id,
                "status": "failed" if is_failure else "done",
                "result": {
                    "problem_type": result.problem_type,
                    "root_cause": result.root_cause,
                    "confidence": str(result.confidence),
                    "key_evidence": result.key_evidence if isinstance(result.key_evidence, list) else [],
                    "user_reply": result.user_reply,
                    "needs_engineer": result.needs_engineer,
                    "rule_type": result.rule_type,
                    "agent_type": result.agent_type,
                },
            }
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(webhook_url, json=payload)
                logger.info("Webhook callback to %s: %d", webhook_url, resp.status_code)
        except Exception as e:
            logger.warning("Webhook callback failed: %s", e)
