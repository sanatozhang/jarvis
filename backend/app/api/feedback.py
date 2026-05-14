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

import asyncio

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile, HTTPException

from app.config import get_settings
from app.db import database as db
from app.services.zendesk import extract_ticket_id, fetch_ticket_with_comments
from app.services.summarize import summarize_ticket_conversation

# Process-local lock to prevent the same client double-clicking submit (or
# browser auto-retry) from racing two pipelines for the same issue. The DB-level
# check below catches cross-process duplicates.
_submit_lock = asyncio.Lock()

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
    occurred_at: str = Form("", description="问题发生时间 (ISO 格式)"),
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

        # Save uploaded files (logs go to raw/, images go to images/)
        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        upload_dir = Path(settings.storage.workspace_dir) / record_id / "raw"
        images_dir = Path(settings.storage.workspace_dir) / record_id / "images"
        upload_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        from app.services.decrypt import _PLAUD_MAGIC, _PLAUD_MAGIC_SCAN_BYTES

        saved_files = []
        for f in log_files:
            if f.filename and f.size and f.size > 0:
                ext = Path(f.filename).suffix.lower()
                is_image = ext in _IMAGE_EXTS
                dest_dir = images_dir if is_image else upload_dir
                dest = dest_dir / f.filename
                content = await f.read()

                # 方案 B：上传边界对 .plaud 文件做 magic 校验
                # - 干净 → 原样保存
                # - 头部污染（CRLF 注入等，但 magic 还在前 16 字节内）→ 自动剥离，保存干净版本 + 日志告警
                # - 完全无 magic → 400 拒收，让用户重传
                if ext == ".plaud" and not is_image:
                    if not content.startswith(_PLAUD_MAGIC):
                        scan = content[:_PLAUD_MAGIC_SCAN_BYTES]
                        offset = scan.find(_PLAUD_MAGIC)
                        if offset > 0:
                            polluted_prefix = content[:offset]
                            logger.warning(
                                "Upload pollution detected on %s: stripped %d-byte prefix %s before plaud magic",
                                f.filename, offset, polluted_prefix.hex(),
                            )
                            content = content[offset:]
                        elif offset < 0:
                            logger.error(
                                "Upload reject %s: not a valid .plaud file (head=%s)",
                                f.filename, content[:16].hex(),
                            )
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f".plaud 文件 {f.filename} 不是有效的 Plaud 加密日志（缺少 magic 字节）。"
                                    "请通过 APP 内『设置 > 意见反馈 / 发送日志』直接上传，"
                                    "避免转发、邮件附件、改后缀等可能破坏文件的操作。"
                                ),
                            )

                with open(dest, "wb") as out:
                    out.write(content)
                saved_files.append({
                    "name": f.filename,
                    "token": "",
                    "size": len(content),
                    "local_path": str(dest),
                })
                logger.info("Saved uploaded file [%s]: %s (%d bytes)", "image" if is_image else "log", f.filename, len(content))

        # Build full description
        desc_parts = []
        if platform:
            desc_parts.append(f"[{platform}]")
        if category:
            desc_parts.append(f"[{category}]")
        desc_parts.append(description)
        full_description = " ".join(desc_parts)

        # Refuse to start a parallel analysis pipeline for an issue already in flight.
        # Two concurrent claude subprocesses on the same logs starve each other on
        # CPU/network and both hit the 600s timeout (observed 2026-04-29 fb_9d56ec4516).
        async with _submit_lock:
            from sqlalchemy import select
            async with db.get_session() as session:
                stmt = select(db.TaskRecord).where(
                    db.TaskRecord.issue_id == record_id,
                    db.TaskRecord.status.in_(["queued", "analyzing", "downloading", "decrypting", "extracting"]),
                ).limit(1)
                in_flight = (await session.execute(stmt)).scalar_one_or_none()
                if in_flight:
                    logger.warning("Duplicate feedback submit for %s — task %s still in %s", record_id, in_flight.id, in_flight.status)
                    raise HTTPException(
                        status_code=409,
                        detail=f"该工单正在分析中（task={in_flight.id}），请等待结果或刷新页面。",
                    )

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
            "source": "local",
            "feishu_link": "",
            "platform": platform,
            "category": category,
            "created_by": username,
            "occurred_at": datetime.fromisoformat(occurred_at) if occurred_at else None,
            "created_at_ms": int(datetime.utcnow().timestamp() * 1000),
            "log_files": saved_files,
        }
        await db.upsert_issue(issue_data, status="analyzing")

        # Create task and start analysis in background
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        await db.create_task(task_id=task_id, issue_id=record_id)

        from app.api.tasks import _run_task
        background_tasks.add_task(_run_task, task_id=task_id, issue_id=record_id, username=username)

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
        import os
        if not os.environ.get("ZENDESK_EMAIL") or not os.environ.get("ZENDESK_API_TOKEN"):
            raise HTTPException(status_code=503, detail="ZENDESK_NOT_CONFIGURED")

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


# ---------------------------------------------------------------------------
# T3 客服反馈闭环 — 让"AI 工程师标签准不准"从拍脑袋变成可量化数据
# ---------------------------------------------------------------------------
from pydantic import BaseModel
from sqlalchemy import select, update, and_


class EngineerLabelFeedbackRequest(BaseModel):
    issue_id: str
    task_id: str = ""                # 可选：精确到某次分析
    actually_needed_engineer: bool   # True=确实需要 / False=AI 误判无需
    feedback_by: str = ""            # 客服用户名
    note: str = ""                   # 可选备注


@router.post("/engineer-label", tags=["Feedback"])
async def submit_engineer_label_feedback(req: EngineerLabelFeedbackRequest):
    """客服在工单详情页对 AI 的 needs_engineer 标签做事后纠偏。

    标签语义：
    - actually_needed_engineer=True : 实际确实需要研发介入（AI 标 false 时为漏报）
    - actually_needed_engineer=False: 实际不需要（AI 标 true 时为误报）

    用途：长期校准 + few-shot 训练样本来源；通过 /api/analytics/engineer-label-accuracy 可看 precision/recall。
    """
    async with db.get_session() as session:
        # 定位 analysis：优先精确 task_id，没给就拿最新一条
        stmt = select(db.AnalysisRecord).where(db.AnalysisRecord.issue_id == req.issue_id)
        if req.task_id:
            stmt = stmt.where(db.AnalysisRecord.task_id == req.task_id)
        stmt = stmt.order_by(db.AnalysisRecord.created_at.desc()).limit(1)
        analysis = (await session.execute(stmt)).scalar_one_or_none()
        if analysis is None:
            raise HTTPException(status_code=404, detail=f"No analysis found for issue_id={req.issue_id}")

        analysis.engineer_label_feedback = req.actually_needed_engineer
        analysis.engineer_label_feedback_by = req.feedback_by[:64]
        analysis.engineer_label_feedback_at = datetime.utcnow()
        analysis.engineer_label_feedback_note = req.note[:1000]
        await session.commit()
        analysis_id = analysis.id
        ai_said = bool(analysis.needs_engineer)

    # 同时打 event，方便 analytics 聚合
    await db.log_event(
        event_type="engineer_label_feedback",
        issue_id=req.issue_id,
        username=req.feedback_by,
        detail={
            "ai_needs_engineer": ai_said,
            "actually_needed_engineer": req.actually_needed_engineer,
            "matched": ai_said == req.actually_needed_engineer,
            "note": req.note[:200],
        },
    )

    return {
        "status": "ok",
        "analysis_id": analysis_id,
        "ai_needs_engineer": ai_said,
        "actually_needed_engineer": req.actually_needed_engineer,
        "matched": ai_said == req.actually_needed_engineer,
    }
