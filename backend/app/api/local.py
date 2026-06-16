"""
API routes for locally-tracked issues (analyzed by Jarvis).

- 进行中: issues.status = 'analyzing'
- 已完成: issues.status = 'done'
- 失败:   issues.status = 'failed'
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func

from app.config import get_settings
from app.db import database as db
from app.services.feishu_cli import FeishuCLI, add_members_to_chat, create_escalation_group, is_feishu_source

logger = logging.getLogger("jarvis.api.local")
router = APIRouter()

# Per-issue locks to serialize escalation: prevents a double-click / concurrent
# escalate from creating two Feishu groups (TOCTOU on escalation_chat_id).
_escalate_locks: dict[str, asyncio.Lock] = {}


def _get_escalate_lock(issue_id: str) -> asyncio.Lock:
    lock = _escalate_locks.get(issue_id)
    if lock is None:
        lock = asyncio.Lock()
        _escalate_locks[issue_id] = lock
    return lock


def _handle_exceptions(label: str):
    """Decorator that catches non-HTTP exceptions, logs them, and raises HTTP 500."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as e:
                logger.error("%s: %s", label, e)
                raise HTTPException(status_code=500, detail=str(e))
        return wrapper
    return decorator


def _paginated_response(items: list, total: int, page: int, page_size: int) -> dict:
    """Build the standard paginated response envelope."""
    return {
        "issues": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/in-progress")
@_handle_exceptions("Failed to list in-progress issues")
async def list_in_progress(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues currently being analyzed (only 'analyzing' status)."""
    items, total = await db.get_local_issues_paginated("analyzing", page, page_size)
    return _paginated_response(items, total, page, page_size)


@router.get("/completed")
@_handle_exceptions("Failed to list completed issues")
async def list_completed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues where AI analysis finished (success or failure)."""
    items, total = await db.get_local_issues_paginated("done,failed", page, page_size)
    return _paginated_response(items, total, page, page_size)


@router.get("/failed")
@_handle_exceptions("Failed to list failed issues")
async def list_failed(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues where analysis failed (from local DB)."""
    items, total = await db.get_local_issues_paginated("failed", page, page_size)
    return _paginated_response(items, total, page, page_size)


@router.get("/tracking")
@_handle_exceptions("Failed to list tracked issues")
async def list_all_tracked(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    created_by: Optional[str] = Query(None, description="Filter by creator"),
    platform: Optional[str] = Query(None, description="Filter by platform: APP/Web/Desktop"),
    category: Optional[str] = Query(None, description="Filter by problem category (partial match)"),
    status: Optional[str] = Query(None, alias="status", description="Filter by status: analyzing/done/failed"),
    source: str = Query("", description="来源: feishu / local / linear / api"),
    zendesk_id: Optional[str] = Query(None, description="Filter by Zendesk ticket number (partial match)"),
    date_from: Optional[str] = Query(None, description="From date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="To date YYYY-MM-DD"),
):
    """List ALL locally-tracked issues with multi-filter support."""
    items, total = await db.get_tracked_issues_paginated(
        page, page_size,
        created_by=created_by, platform=platform, category=category,
        status_filter=status, source=source or None, zendesk_id=zendesk_id, date_from=date_from, date_to=date_to,
    )
    return _paginated_response(items, total, page, page_size)


@router.get("/inaccurate")
@_handle_exceptions("Failed to list inaccurate issues")
async def list_inaccurate(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Get issues marked as inaccurate."""
    items, total = await db.get_local_issues_paginated("inaccurate", page, page_size)
    return _paginated_response(items, total, page, page_size)


@router.get("/{issue_id}/analyses")
@_handle_exceptions("Failed to get analyses")
async def get_issue_analyses(issue_id: str):
    """Get ALL analyses for an issue, ordered newest first."""
    analyses = await db.get_all_analyses_by_issue(issue_id)
    return [
        {
            "task_id": a.task_id,
            "issue_id": a.issue_id,
            "problem_type": a.problem_type or "",
            "problem_type_en": a.problem_type_en or "",
            "problem_categories": json.loads(a.problem_categories_json) if a.problem_categories_json else [],
            "device_type": a.device_type or "",
            "root_cause": a.root_cause or "",
            "root_cause_en": a.root_cause_en or "",
            "confidence": a.confidence or "medium",
            "confidence_reason": a.confidence_reason or "",
            "key_evidence": json.loads(a.key_evidence_json) if a.key_evidence_json else [],
            "user_reply": a.user_reply or "",
            "user_reply_en": a.user_reply_en or "",
            "needs_engineer": a.needs_engineer,
            "system_failure": getattr(a, "system_failure", False) or False,
            "needs_user_retry": getattr(a, "needs_user_retry", False) or False,
            "engineer_label_feedback": getattr(a, "engineer_label_feedback", None),
            "engineer_label_feedback_by": getattr(a, "engineer_label_feedback_by", "") or "",
            "engineer_label_feedback_at": (
                (a.engineer_label_feedback_at.isoformat() + "Z")
                if getattr(a, "engineer_label_feedback_at", None) else ""
            ),
            "engineer_label_feedback_note": getattr(a, "engineer_label_feedback_note", "") or "",
            "fix_suggestion": a.fix_suggestion or "",
            "rule_type": a.rule_type or "",
            "agent_type": a.agent_type or "",
            "agent_model": getattr(a, "agent_model", "") or "",
            "followup_question": a.followup_question or "",
            "log_metadata": json.loads(a.log_metadata_json) if getattr(a, "log_metadata_json", None) else {},
            "created_at": (a.created_at.isoformat() + "Z") if a.created_at else "",
        }
        for a in analyses
    ]


@router.get("/{issue_id}/detail")
@_handle_exceptions("Failed to get issue detail")
async def get_issue_detail(issue_id: str):
    """Get a single issue with its analysis and task data by ID."""
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        a_stmt = select(db.AnalysisRecord).where(
            db.AnalysisRecord.issue_id == issue_id
        ).order_by(db.AnalysisRecord.created_at.desc()).limit(1)
        analysis = (await session.execute(a_stmt)).scalar_one_or_none()

        a_count_stmt = select(func.count()).select_from(db.AnalysisRecord).where(db.AnalysisRecord.issue_id == issue_id)
        a_count = (await session.execute(a_count_stmt)).scalar() or 0

        t_stmt = select(db.TaskRecord).where(
            db.TaskRecord.issue_id == issue_id
        ).order_by(db.TaskRecord.created_at.desc()).limit(1)
        task = (await session.execute(t_stmt)).scalar_one_or_none()

        return db._issue_to_dict(issue, analysis=analysis, task=task, analysis_count=a_count)


@router.get("/{issue_id}/files/{filename:path}")
@_handle_exceptions("Failed to serve issue file")
async def serve_issue_file(issue_id: str, filename: str):
    """Serve a file from workspace, or download from Feishu on demand."""
    settings = get_settings()

    search_dirs = [
        Path(settings.storage.workspace_dir) / issue_id / "raw",
        Path(settings.storage.workspace_dir) / issue_id / "processed",
    ]

    async with db.get_session() as session:
        stmt = select(db.TaskRecord).where(db.TaskRecord.issue_id == issue_id).order_by(db.TaskRecord.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        task = result.scalar_one_or_none()
        if task:
            search_dirs.insert(0, Path(settings.storage.workspace_dir) / task.id / "raw")
            search_dirs.insert(1, Path(settings.storage.workspace_dir) / task.id / "images")

    for d in search_dirs:
        file_path = d / filename
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)

    # Not found locally — try downloading from Feishu if this is a feishu-sourced issue
    if is_feishu_source(issue_id):
        try:
            cli = FeishuCLI()
            rec = await cli.get_record(issue_id)
            fields = rec.get("fields", {})
            # Search both 日志文件 and 其他附件 for matching filename
            for field_name in ("日志文件", "其他附件"):
                for f in (fields.get(field_name) or []):
                    if isinstance(f, dict) and f.get("name") == filename and f.get("file_token"):
                        cache_dir = Path(settings.storage.workspace_dir) / "_cache" / issue_id / "raw"
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        save_path = str(cache_dir / filename)
                        await cli.download_file(f["file_token"], save_path)
                        return FileResponse(save_path)
        except Exception as e:
            logger.warning("Failed to download %s from Feishu for %s: %s", filename, issue_id, e)

    raise HTTPException(status_code=404, detail="File not found")


@router.get("/{issue_id}/download-logs")
@_handle_exceptions("Failed to download logs")
async def download_logs(issue_id: str):
    """Download log files for an issue — returns the user's ORIGINAL upload.

    Lookup order (priority: hand back what the user gave us):
      1. Raw uploaded files in issue or task workspace raw/ — primary
         (.plaud encrypted source, .log, .zip, anything they uploaded).
         Matches the size shown on the issue detail page (feedback.py records
         `size=len(content)` from the raw upload).
      2. Decrypted .log/.txt in task workspace logs/ or issue workspace
         processed/ — fallback only when raw is absent (e.g. old issues whose
         raw was cleaned, or Linear/Feishu imports without a cached upload).

    Historical bug (fixed 2026-05-25 — see fb_78d0606c6d / fb_12a7b3d4d0):
    the order used to be inverted — we returned the 271 KB windowed plaud.log
    for an issue whose UI advertised a 53 MB .plaud, leaving users confused
    about whether the file got corrupted. The decrypted artefact is an
    internal AI input, not a user-visible product.

    Single file → direct download; multiple → zipped.
    """
    import io
    import zipfile

    settings = get_settings()
    workspace_dir = Path(settings.storage.workspace_dir)

    task_id: Optional[str] = None
    async with db.get_session() as session:
        stmt = select(db.TaskRecord).where(
            db.TaskRecord.issue_id == issue_id
        ).order_by(db.TaskRecord.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        task = result.scalar_one_or_none()
        if task:
            task_id = task.id

    log_files: list[Path] = []
    seen_names: set[str] = set()

    # Tier 1: raw uploaded files (what user actually uploaded — primary).
    # Note feedback.py routes images to images/ at upload time, so iterating
    # raw/ here is naturally log-only.
    raw_dirs: list[Path] = [workspace_dir / issue_id / "raw"]
    if task_id:
        raw_dirs.append(workspace_dir / task_id / "raw")

    for d in raw_dirs:
        if not d.exists():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.name not in seen_names:
                log_files.append(f)
                seen_names.add(f.name)

    # Tier 2: fall back to decrypted .log/.txt only when raw is gone.
    if not log_files:
        decrypted_dirs: list[Path] = []
        if task_id:
            decrypted_dirs.append(workspace_dir / task_id / "logs")
        decrypted_dirs.append(workspace_dir / issue_id / "processed")

        for d in decrypted_dirs:
            if not d.exists():
                continue
            for f in sorted(d.iterdir()):
                if f.is_file() and f.name not in seen_names and f.suffix.lower() in (".log", ".txt"):
                    log_files.append(f)
                    seen_names.add(f.name)

    if not log_files:
        raise HTTPException(
            status_code=404,
            detail="No log files found (neither decrypted logs nor raw .plaud source)",
        )

    # Single file → direct download with proper Content-Disposition
    if len(log_files) == 1:
        return FileResponse(
            log_files[0],
            filename=log_files[0].name,
            media_type="application/octet-stream",
        )

    # Multiple files → ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in log_files:
            zf.write(f, f.name)
    buf.seek(0)

    filename = f"logs_{issue_id}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{issue_id}")
@_handle_exceptions("Failed to delete issue")
async def delete_issue(issue_id: str):
    """Soft-delete an issue (mark as deleted, hide from UI)."""
    ok = await db.soft_delete_issue(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Issue not found")
    return {"status": "deleted", "issue_id": issue_id}


class EscalateRequest(BaseModel):
    note: str = ""
    escalated_by: str = ""
    escalated_by_email: str = ""  # 点击转交者的 plaud 邮箱（用于自动拉其进群）
    appllo_url: str = ""  # Frontend passes the full issue URL


@router.post("/{issue_id}/escalate")
@_handle_exceptions("Failed to escalate issue")
async def escalate_issue(issue_id: str, body: EscalateRequest):
    """Escalate an issue to engineering team — creates a Feishu group chat.

    Idempotent: if the issue is already escalated and has a chat group, return
    the existing chat info without creating a duplicate group.
    """
    async with db.get_session() as session:
        issue_rec = await session.get(db.IssueRecord, issue_id)
    if not issue_rec:
        raise HTTPException(status_code=404, detail="Issue not found")

    # 解析点击转交者的 plaud 邮箱：优先前端直传（登录态已有），回退 user 表 feishu_email。
    # 这是"建群人自动进群"的关键——之前只传 username，查不到 email 就没被拉进群。
    user = await db.get_user(body.escalated_by) if body.escalated_by else None
    user_email = (body.escalated_by_email or "").strip() or (user or {}).get("feishu_email", "")

    # Serialize per-issue so a double-click / concurrent escalate can't create two
    # Feishu groups: re-read state INSIDE the lock and short-circuit if a group
    # already exists (the racing request that lost will see the winner's chat_id).
    async with _get_escalate_lock(issue_id):
        async with db.get_session() as session:
            issue_rec = await session.get(db.IssueRecord, issue_id)
        if not issue_rec:
            raise HTTPException(status_code=404, detail="Issue not found")

        # Short-circuit: already escalated with an active group → return existing info
        if issue_rec.escalated_at and issue_rec.escalation_chat_id:
            # 群已存在：把当前点击的人也拉进群（"点击即加入"），非致命
            if user_email:
                try:
                    await add_members_to_chat(issue_rec.escalation_chat_id, [user_email])
                except Exception as e:
                    logger.warning("Failed to add clicker %s to existing group: %s", user_email, e)
            return {
                "status": "escalated",
                "issue_id": issue_id,
                "chat_id": issue_rec.escalation_chat_id or "",
                "share_link": issue_rec.escalation_share_link or "",
                "group_exists": True,
            }

        description = issue_rec.description or issue_id
        problem_type = ""
        analysis = await db.get_analysis_by_issue(issue_id)
        if analysis:
            # 优先用英文字段；中文 fallback 通过映射表翻译
            _pt_en = (analysis.problem_type_en or "").strip()
            _pt_zh = (analysis.problem_type or "").strip()
            _ZH_TO_EN = {
                "未知": "Unknown", "蓝牙连接": "Bluetooth Connection",
                "固件升级": "Firmware Upgrade", "时间戳问题": "Timestamp Issue",
                "录音问题": "Recording Issue", "设备故障": "Device Failure",
                "文件传输": "File Transfer", "云同步": "Cloud Sync",
                "转写问题": "Transcription Issue", "软件bug": "Software Bug",
                "用户操作": "User Operation", "会员与支付": "Membership & Payment",
                "其他": "Other",
            }
            problem_type = _pt_en or _ZH_TO_EN.get(_pt_zh, _pt_zh)

        issue_link = ""
        if is_feishu_source(issue_id):
            issue_link = FeishuCLI().get_feishu_link(issue_id)

        appllo_url = body.appllo_url or ""

        # user_email 已在上方解析（优先前端直传 plaud 邮箱）

        chat_result = None

        # Create Feishu escalation group + add members + notify
        try:
            chat_result = await create_escalation_group(
                user_email=user_email,
                issue_id=issue_id,
                description=description,
                problem_type=problem_type,
                issue_link=issue_link,
                zendesk_id=issue_rec.zendesk_id or "",
                appllo_url=appllo_url,
            )
            logger.info("Escalation completed: %s", chat_result)
        except Exception as e:
            logger.error("Failed to create escalation group: %s", e)
            # Fallback DM gated by ENABLE_ONCALL_NOTIFY (default off)
            import os
            if os.environ.get("ENABLE_ONCALL_NOTIFY", "false").lower() == "true":
                try:
                    from app.services.notify import notify_oncall
                    await notify_oncall(
                        issue_id=issue_id,
                        description=description,
                        reason=f"工单转交工程师: {problem_type}" if problem_type else "工单转交工程师",
                        link=issue_link,
                    )
                except Exception as ne:
                    logger.error("Fallback notify_oncall also failed: %s", ne)

        # Save escalation metadata (including chat_id + share_link for later notifications and "join group" button)
        escalation_chat_id = chat_result.get("chat_id", "") if chat_result else ""
        escalation_share_link = chat_result.get("share_link", "") if chat_result else ""
        ok = await db.escalate_issue(
            issue_id,
            escalated_by=body.escalated_by,
            note=body.note,
            chat_id=escalation_chat_id,
            share_link=escalation_share_link,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Issue not found")
        await db.log_event("escalate", issue_id=issue_id, username=body.escalated_by,
                           detail={"note": body.note, "chat_id": escalation_chat_id})

        result = {"status": "escalated", "issue_id": issue_id}
        if chat_result:
            result["chat_id"] = chat_result.get("chat_id", "")
            result["group_name"] = chat_result.get("group_name", "")
            result["share_link"] = chat_result.get("share_link", "")
        return result


@router.post("/{issue_id}/inaccurate")
@_handle_exceptions("Failed to mark inaccurate")
async def mark_inaccurate(issue_id: str):
    """Mark an issue's analysis as inaccurate."""
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

    await db.update_issue_status(issue_id, "inaccurate")
    await db.log_event("mark_inaccurate", issue_id=issue_id)
    return {"status": "ok"}


class MarkCompleteRequest(BaseModel):
    username: str = ""


@router.post("/{issue_id}/complete")
@_handle_exceptions("Failed to mark complete")
async def mark_complete(issue_id: str, body: MarkCompleteRequest):
    """Mark issue as completed — syncs to Feishu if feishu-sourced."""
    async with db.get_session() as session:
        issue = await session.get(db.IssueRecord, issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

    await db.update_issue_status(issue_id, "done")
    await db.log_event("mark_complete", issue_id=issue_id, username=body.username)

    # Sync to Feishu bitable: only set 确认提交=true (don't touch other fields)
    feishu_synced = False
    if is_feishu_source(issue_id):
        try:
            await FeishuCLI().update_record(issue_id, {"确认提交": True})
            feishu_synced = True
            logger.info("Feishu issue %s marked as completed", issue_id)
        except Exception as e:
            logger.error("Failed to sync completion to Feishu for %s: %s", issue_id, e)

    # 若该工单已 escalate（建过飞书群），同步 resolve + 在群里发完成通知。
    # 复用 oncall resolve 同一条逻辑，避免两套实现漂移（详情页按钮历史上完全漏掉了这步）。
    from app.services.feishu_cli import resolve_escalation_and_notify
    esc = await resolve_escalation_and_notify(issue_id)

    return {
        "status": "done",
        "issue_id": issue_id,
        "feishu_synced": feishu_synced,
        "feishu_notified": esc["feishu_notified"],
    }
