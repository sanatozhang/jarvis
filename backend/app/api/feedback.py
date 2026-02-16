"""
API routes for user feedback / manual issue submission.
Submitting feedback immediately triggers AI analysis.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile, HTTPException

from app.config import get_settings
from app.db import database as db
from app.services.zendesk import extract_ticket_id, fetch_ticket_with_comments
from app.services.summarize import summarize_ticket_conversation

logger = logging.getLogger("jarvis.api.feedback")
router = APIRouter()


@router.post("")
async def submit_feedback(
    background_tasks: BackgroundTasks,
    description: str = Form(..., description="问题描述"),
    category: str = Form("", description="问题分类"),
    device_sn: str = Form("", description="设备 SN"),
    firmware: str = Form("", description="固件版本号"),
    app_version: str = Form("", description="APP 版本"),
    platform: str = Form("APP", description="平台: APP / Web / Desktop"),
    priority: str = Form("L", description="优先级: H / L"),
    zendesk: str = Form("", description="Zendesk 工单号或链接"),
    username: str = Form("", description="提交人"),
    log_files: list[UploadFile] = File(default=[], description="日志文件"),
):
    """
    Submit feedback → save to DB → immediately start AI analysis.
    """
    try:
        settings = get_settings()
        record_id = f"fb_{uuid.uuid4().hex[:10]}"

        # Normalize zendesk
        zendesk_url = ""
        zendesk_id = ""
        if zendesk:
            m = re.search(r"#?(\d{4,})", zendesk)
            if m:
                ticket_num = m.group(1)
                zendesk_id = f"#{ticket_num}"
                zendesk_url = f"https://nicebuildllc.zendesk.com/agent/tickets/{ticket_num}" if not zendesk.startswith("http") else zendesk

        # Save uploaded files
        upload_dir = Path(settings.storage.workspace_dir) / record_id / "raw"
        upload_dir.mkdir(parents=True, exist_ok=True)

        saved_files = []
        for f in log_files:
            if f.filename and f.size and f.size > 0:
                dest = upload_dir / f.filename
                content = await f.read()
                with open(dest, "wb") as out:
                    out.write(content)
                saved_files.append({
                    "name": f.filename,
                    "token": "",
                    "size": len(content),
                    "local_path": str(dest),
                })
                logger.info("Saved uploaded file: %s (%d bytes)", f.filename, len(content))

        # Build full description
        desc_parts = []
        if platform:
            desc_parts.append(f"[{platform}]")
        if category:
            desc_parts.append(f"[{category}]")
        desc_parts.append(description)
        full_description = " ".join(desc_parts)

        # Save to DB as "analyzing" (immediately start analysis)
        issue_data = {
            "record_id": record_id,
            "description": full_description,
            "device_sn": device_sn,
            "firmware": firmware,
            "app_version": app_version,
            "priority": priority,
            "zendesk": zendesk_url,
            "zendesk_id": zendesk_id,
            "feishu_link": "",
            "platform": platform,
            "category": category,
            "created_at_ms": int(datetime.utcnow().timestamp() * 1000),
            "log_files": saved_files,
        }
        await db.upsert_issue(issue_data, status="analyzing")
        if username:
            await db.set_issue_created_by(record_id, username)

        # Create task and start analysis in background
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        await db.create_task(task_id=task_id, issue_id=record_id)

        from app.api.tasks import _run_task
        background_tasks.add_task(_run_task, task_id=task_id, issue_id=record_id)

        logger.info("Feedback submitted and analysis started: %s task=%s", record_id, task_id)

        # Track: feedback submitted
        await db.log_event("feedback_submit", issue_id=record_id, username=username, detail={"platform": platform, "category": category, "has_logs": len(saved_files) > 0})

        return {
            "status": "ok",
            "record_id": record_id,
            "task_id": task_id,
            "files_uploaded": len(saved_files),
            "message": "反馈已提交，AI 分析已启动",
        }
    except Exception as e:
        logger.error("Feedback submission failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/import-zendesk")
async def import_from_zendesk(zendesk_input: str = Form(..., description="Zendesk 工单号或链接")):
    """
    Import data from a Zendesk ticket:
    1. Fetch ticket + comments from Zendesk API
    2. Summarize conversation using ChatGPT
    3. Return pre-filled form data for the feedback page
    """
    try:
        ticket_id = extract_ticket_id(zendesk_input)
        if not ticket_id:
            raise HTTPException(status_code=400, detail="无法识别 Zendesk 工单号")

        # Fetch ticket + comments
        ticket_data = await fetch_ticket_with_comments(ticket_id, max_comments=50)
        logger.info("Fetched Zendesk ticket #%s: %d comments", ticket_id, ticket_data["comment_count"])

        # Summarize with ChatGPT
        summary = await summarize_ticket_conversation(
            ticket_subject=ticket_data["subject"],
            comments=ticket_data["comments"],
        )

        return {
            "status": "ok",
            "ticket_id": ticket_id,
            "ticket_subject": ticket_data["subject"],
            "comment_count": ticket_data["comment_count"],
            "zendesk_url": f"https://nicebuildllc.zendesk.com/agent/tickets/{ticket_id}",
            # AI-filled fields (user can modify)
            "description": summary.get("description", ""),
            "category": summary.get("category", ""),
            "priority": summary.get("priority", "L"),
            "device_sn": summary.get("device_sn", ""),
            "firmware": summary.get("firmware", ""),
            "app_version": summary.get("app_version", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Zendesk import failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
