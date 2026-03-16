"""
Linear webhook handler.

Receives webhook events from Linear, detects @ai-agent trigger in comments,
and launches the AI analysis pipeline. Results are posted back as comments.

Webhook setup in Linear:
  Settings → API → Webhooks → Create webhook
  URL: https://<your-domain>/api/linear/webhook
  Events: Comment (create)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import get_settings
from app.db import database as db
from app.models.schemas import AnalysisResult, Issue
from app.services.linear import (
    LinearClient,
    format_analysis_comment,
    verify_webhook_signature,
)

logger = logging.getLogger("jarvis.api.linear")
router = APIRouter()

# Track in-flight analyses to avoid duplicate triggers
_active_issues: set[str] = set()

# Followup trigger keyword — must be checked BEFORE the main trigger
# because "@ai-agent" is a prefix of "@ai-agent-followup"
_FOLLOWUP_TRIGGER = "@ai-agent-followup"


@router.post("/webhook")
async def handle_linear_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive and process Linear webhook events.

    Supported events:
    - Comment create: triggers analysis if comment body contains @ai-agent
    - Issue create: triggers analysis if issue description contains @ai-agent
    """
    settings = get_settings()
    body = await request.body()

    # --- Signature verification ---
    if settings.linear.webhook_secret:
        signature = request.headers.get("Linear-Signature", "")
        if not verify_webhook_signature(body, signature, settings.linear.webhook_secret):
            logger.warning("Linear webhook signature verification failed")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()

    action = payload.get("action")
    event_type = payload.get("type")
    data = payload.get("data", {})

    logger.info(
        "=== Linear Webhook Received ===\n"
        "  type: %s\n"
        "  action: %s\n"
        "  data keys: %s\n"
        "  data.id: %s\n"
        "  data.issueId: %s",
        event_type, action,
        list(data.keys()),
        data.get("id", ""),
        data.get("issueId", ""),
    )

    # --- Handle Comment events ---
    if event_type == "Comment" and action == "create":
        await _handle_comment_create(data, payload, background_tasks)

    # --- Handle Issue events (check description for trigger keyword) ---
    elif event_type == "Issue" and action == "create":
        await _handle_issue_create(data, payload, background_tasks)

    return {"status": "ok"}


async def _handle_comment_create(
    comment_data: Dict[str, Any],
    full_payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
):
    """Process a new comment — check for trigger keyword and launch analysis."""
    settings = get_settings()
    trigger = settings.linear.trigger_keyword.lower()

    body = (comment_data.get("body") or "").strip()
    comment_id = comment_data.get("id", "")
    issue_id = comment_data.get("issueId") or comment_data.get("issue", {}).get("id", "")
    user_name = comment_data.get("user", {}).get("name", "") if isinstance(comment_data.get("user"), dict) else ""

    logger.info(
        "  [Comment] id=%s issueId=%s user=%s body_preview='%s'",
        comment_id, issue_id, user_name, body[:120],
    )

    if not issue_id:
        logger.warning("  → Comment missing issueId, skipping")
        return

    if issue_id in _active_issues:
        logger.info("  → Analysis already in progress for issue %s, skipping", issue_id)
        return

    # --- Check followup trigger FIRST (it starts with the main trigger keyword) ---
    if _FOLLOWUP_TRIGGER in body.lower():
        followup_question = _extract_followup_question(body)
        _active_issues.add(issue_id)
        logger.info(
            "  → Followup trigger matched! issue=%s user=%s question='%s'",
            issue_id, user_name or "unknown", followup_question[:80],
        )
        background_tasks.add_task(
            _run_linear_analysis,
            linear_issue_id=issue_id,
            trigger_comment_id=comment_id,
            trigger_body=body,
            trigger_user=user_name,
            followup_question=followup_question,
        )
        return

    # --- Check main trigger ---
    if trigger not in body.lower():
        logger.info("  → No trigger keyword found, skipping")
        return

    _active_issues.add(issue_id)
    logger.info("  → Trigger matched! Launching analysis for issue %s by %s", issue_id, user_name or "unknown")

    # Launch analysis in background
    background_tasks.add_task(
        _run_linear_analysis,
        linear_issue_id=issue_id,
        trigger_comment_id=comment_id,
        trigger_body=body,
        trigger_user=user_name,
    )


async def _handle_issue_create(
    issue_data: Dict[str, Any],
    full_payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
):
    """Process a new issue — check description for trigger keyword and launch analysis."""
    settings = get_settings()
    trigger = settings.linear.trigger_keyword.lower()

    title = (issue_data.get("title") or "").strip()
    description = (issue_data.get("description") or "").strip()
    combined = f"{title} {description}".lower()

    if trigger not in combined:
        logger.debug("Issue does not contain trigger keyword '%s', skipping", trigger)
        return

    issue_id = issue_data.get("id", "")
    if not issue_id:
        logger.warning("Issue webhook missing id, skipping")
        return

    if issue_id in _active_issues:
        logger.info("Analysis already in progress for issue %s, skipping", issue_id)
        return

    _active_issues.add(issue_id)

    # Remove trigger keyword from description so it doesn't pollute the analysis
    trigger_keyword = settings.linear.trigger_keyword
    clean_description = description.replace(trigger_keyword, "").strip()

    # Extract the issue creator as the trigger user
    creator = issue_data.get("creator", {})
    trigger_user = creator.get("name", "") if isinstance(creator, dict) else ""

    logger.info(
        "Trigger detected in issue! issue=%s title='%s' creator='%s'",
        issue_id, title[:80], trigger_user or "unknown",
    )

    background_tasks.add_task(
        _run_linear_analysis,
        linear_issue_id=issue_id,
        trigger_comment_id="",
        trigger_body=clean_description,
        trigger_user=trigger_user,
    )


async def _run_linear_analysis(
    linear_issue_id: str,
    trigger_comment_id: str,
    trigger_body: str,
    trigger_user: str = "",
    followup_question: str = "",
):
    """
    Full analysis pipeline for a Linear issue:
    1. Fetch issue details from Linear API
    2. Download attachments (log files)
    3. Run analysis pipeline
    4. Post result as comment
    """
    client = LinearClient()
    settings = get_settings()

    try:
        # --- Step 1: Fetch issue details ---
        logger.info("Fetching Linear issue %s ...", linear_issue_id)
        linear_issue = await client.get_issue(linear_issue_id)

        if not linear_issue:
            logger.error("Linear issue %s not found", linear_issue_id)
            return

        identifier = linear_issue.get("identifier", "")  # e.g. "ENG-123"
        title = linear_issue.get("title", "")
        description = linear_issue.get("description", "")
        issue_url = linear_issue.get("url", "")
        state_name = linear_issue.get("state", {}).get("name", "") if isinstance(linear_issue.get("state"), dict) else ""
        assignee_name = linear_issue.get("assignee", {}).get("name", "") if isinstance(linear_issue.get("assignee"), dict) else ""
        labels = [l.get("name", "") for l in linear_issue.get("labels", {}).get("nodes", [])] if isinstance(linear_issue.get("labels"), dict) else []
        attachments = linear_issue.get("attachments", {}).get("nodes", []) if isinstance(linear_issue.get("attachments"), dict) else []

        logger.info(
            "=== Linear Issue Details ===\n"
            "  identifier: %s\n"
            "  title: %s\n"
            "  url: %s\n"
            "  state: %s\n"
            "  assignee: %s\n"
            "  labels: %s\n"
            "  priority: %s\n"
            "  description length: %d chars\n"
            "  link attachments (GraphQL): %d\n"
            "  trigger comment: %s\n"
            "  (uploaded files will be scanned from description & comments later)",
            identifier, title, issue_url, state_name, assignee_name,
            labels, linear_issue.get("priority"),
            len(description),
            len(attachments),
            trigger_comment_id or "(from issue description)",
        )

        # Post acknowledgement comment
        if followup_question:
            await client.create_comment(
                linear_issue_id,
                f"🤖 **AI follow-up analysis started** for {identifier}.\n> {followup_question[:200]}",
            )
        else:
            await client.create_comment(
                linear_issue_id,
                f"🤖 **AI analysis started** for {identifier}. This may take a few minutes...",
            )

        # --- Step 2: Build internal Issue model ---
        task_id = f"linear_{uuid.uuid4().hex[:12]}"
        record_id = f"lin_{linear_issue_id[:20]}"

        # Parse device info from description (best-effort extraction)
        device_sn = _extract_field(description, ["SN", "sn", "设备SN", "Serial"])
        firmware = _extract_field(description, ["固件", "Firmware", "FW"])
        app_version = _extract_field(description, ["APP", "App版本", "app_version"])
        priority = "H" if (linear_issue.get("priority") or 0) <= 2 else "L"

        # Use trigger comment body as extra context if it has more than just the keyword
        trigger_keyword = settings.linear.trigger_keyword
        extra_context = trigger_body.replace(trigger_keyword, "").strip()
        full_description = f"{title}\n\n{description}"
        if extra_context:
            full_description += f"\n\n[Additional context from comment]: {extra_context}"

        issue = Issue(
            record_id=record_id,
            description=full_description[:2000],
            device_sn=device_sn,
            firmware=firmware,
            app_version=app_version,
            priority=priority,
            source="linear",
            linear_issue_id=identifier,
            linear_issue_url=issue_url,
            linear_comment_id=trigger_comment_id,
        )

        # Save to DB
        author = trigger_user or "linear"
        await db.upsert_issue(issue.model_dump(), status="analyzing")
        await db.set_issue_created_by(record_id, author)
        await db.create_task(task_id=task_id, issue_id=record_id)
        await db.log_event("analysis_start", issue_id=record_id, username=author)
        await db.update_task(task_id, status="analyzing", progress=10, message="获取 Linear 工单信息...")

        # --- Step 3: Download uploaded files ---
        # Linear uploaded files are embedded as markdown URLs in description/comments,
        # NOT in the GraphQL `attachments` field (which is for external link attachments).
        workspace = Path(settings.storage.workspace_dir) / task_id
        raw_dir = workspace / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        uploaded_files = await client.collect_uploaded_files(
            linear_issue_id, description=description,
        )
        downloaded_files = []
        downloaded_images = []
        images_dir = workspace / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        for uf in uploaded_files:
            filename = uf["filename"]
            is_image = uf.get("file_type") == "image"
            save_dir = images_dir if is_image else raw_dir
            save_path = save_dir / filename
            try:
                actual_path = await client.download_attachment(uf["url"], str(save_path))
                actual_path = Path(actual_path)
                if is_image:
                    downloaded_images.append(actual_path)
                else:
                    downloaded_files.append(actual_path)
                logger.info("  Downloaded [%s]: %s → %s (%d bytes)",
                            "image" if is_image else "log", uf["source"], actual_path.name, actual_path.stat().st_size)
            except Exception as e:
                logger.error("  Failed to download '%s' from %s: %s", filename, uf["source"], e)

        # Also try GraphQL attachments as fallback (external link attachments)
        api_attachments = linear_issue.get("attachments", {}).get("nodes", []) if isinstance(linear_issue.get("attachments"), dict) else []
        for att in api_attachments:
            att_url = att.get("url", "")
            if not att_url or att_url in {uf["url"] for uf in uploaded_files}:
                continue
            att_title = att.get("title", "") or "attachment"
            filename = att_title
            if "/" in att_url:
                url_filename = att_url.rsplit("/", 1)[-1].split("?")[0]
                if "." in url_filename:
                    filename = url_filename
            save_path = raw_dir / filename
            try:
                await client.download_attachment(att_url, str(save_path))
                downloaded_files.append(save_path)
                logger.info("  Downloaded API attachment: %s (%d bytes)", filename, save_path.stat().st_size)
            except Exception as e:
                logger.error("  Failed to download API attachment '%s': %s", att_title, e)

        logger.info("  Total downloaded: %d logs, %d images (from %d uploads + %d API attachments)",
                     len(downloaded_files), len(downloaded_images), len(uploaded_files), len(api_attachments))
        await db.update_task(task_id, status="analyzing", progress=25,
                             message=f"已下载 {len(downloaded_files)} 个日志, {len(downloaded_images)} 张图片")

        if not downloaded_files and not downloaded_images:
            logger.warning("No downloadable files for issue %s, analyzing description only", identifier)

        # --- Step 4: Run analysis pipeline ---
        from app.services.decrypt import process_log_file
        from app.services.extractor import extract_for_rules
        from app.services.rule_engine import RuleEngine
        from app.services.agent_orchestrator import AgentOrchestrator
        from app.agents.base import BaseAgent

        # Validate downloaded files
        logger.info("=== Processing downloaded files ===")
        valid_files = []
        for fp in downloaded_files:
            size = fp.stat().st_size if fp.exists() else 0
            # Read first few bytes to check file type
            magic = b""
            if size > 0:
                with open(fp, "rb") as f:
                    magic = f.read(16)
            logger.info("  File: %s (size: %d bytes, magic: %s)", fp.name, size, magic[:8].hex() if magic else "empty")
            if size == 0:
                logger.warning("  ⚠ Skipping empty file: %s", fp.name)
                continue
            # Check if it's actually an HTML error page (not a real file)
            if magic[:5] in (b"<!DOC", b"<html", b"<HTML", b"<?xml"):
                logger.warning("  ⚠ Skipping HTML/XML response (likely error page): %s", fp.name)
                continue
            valid_files.append(fp)

        # Decrypt / process log files
        log_paths = []
        processed_dir = workspace / "processed"
        processed_dir.mkdir(exist_ok=True)

        for fp in valid_files:
            logger.info("  Processing: %s ...", fp.name)
            log_path, incorrect, reason = process_log_file(fp, processed_dir)
            if log_path:
                log_size = log_path.stat().st_size if log_path.exists() else 0
                logger.info("  ✓ Decrypted: %s → %s (%d bytes)", fp.name, log_path, log_size)
                log_paths.append(log_path)
            else:
                logger.warning("  ✗ Failed to process %s: incorrect=%s reason=%s", fp.name, incorrect, reason)

        logger.info("=== Decrypt result: %d/%d files processed successfully ===", len(log_paths), len(valid_files))
        await db.update_task(task_id, status="analyzing", progress=40, message=f"解密完成，{len(log_paths)} 个日志文件")

        # Match rules
        engine = RuleEngine()
        try:
            await engine.sync_files_to_db()
        except Exception:
            pass
        rules = engine.match_rules(full_description)
        rule_type = engine.classify(full_description)

        await db.update_task(task_id, status="analyzing", progress=50, message="AI 分析中...")

        # Pre-extract
        extraction = extract_for_rules(rules, log_paths) if log_paths else {}

        # Prepare workspace
        engine.prepare_workspace(workspace, rules, log_paths)

        # Detect language from issue title
        issue_language = _detect_language(title)
        logger.info("  Detected language from title: %s (title: '%s')", issue_language, title[:50])

        # Load previous analysis for followup
        previous_analysis = None
        if followup_question:
            import json as _json
            prev = await db.get_analysis_by_issue(record_id)
            if prev:
                previous_analysis = {
                    "problem_type": prev.problem_type or "",
                    "root_cause": prev.root_cause or "",
                    "confidence": prev.confidence or "",
                    "key_evidence": _json.loads(prev.key_evidence_json) if prev.key_evidence_json else [],
                    "user_reply": prev.user_reply or "",
                    "fix_suggestion": prev.fix_suggestion or "",
                }
                logger.info("  Loaded previous analysis for followup (type=%s)", prev.problem_type)
            else:
                logger.warning("  No previous analysis found for followup, proceeding without context")

        # Run agent
        prompt = BaseAgent.build_prompt(
            issue=issue,
            rules=rules,
            extraction=extraction,
            language=issue_language,
            previous_analysis=previous_analysis,
            followup_question=followup_question,
        )
        orchestrator = AgentOrchestrator()
        agent = orchestrator.select_agent(rule_type)
        result = await agent.analyze(workspace=workspace, prompt=prompt)
        result.task_id = task_id
        result.issue_id = record_id
        result.rule_type = rule_type
        if followup_question:
            result.followup_question = followup_question

        # --- Step 5: Save result and post comment ---
        is_failure = (
            result.problem_type in ("分析超时", "日志解析失败", "Agent 不可用", "未知")
            or (result.confidence == "low" and result.needs_engineer and not result.user_reply)
            or "未产出结构化结果" in (result.root_cause or "")
        )

        await db.save_analysis(result.model_dump())

        # Calculate duration for analytics
        _start_time = issue.created_at_ms or 0
        _duration_ms = int((datetime.utcnow().timestamp() * 1000) - _start_time) if _start_time else 0

        if is_failure:
            await db.update_task(task_id, status="failed", progress=100, message="分析失败", error=result.root_cause[:200])
            await db.update_issue_status(record_id, "failed")
            await db.log_event("analysis_fail", issue_id=record_id, username=author,
                               duration_ms=_duration_ms, detail={"reason": result.problem_type})

            # Post failure comment
            await client.create_comment(
                linear_issue_id,
                f"🤖 **AI analysis failed** for {identifier}.\n\n"
                f"**Reason**: {result.root_cause[:500]}\n\n"
                f"An engineer may need to review this manually.",
            )
        else:
            await db.update_task(task_id, status="done", progress=100, message="分析完成")
            await db.update_issue_status(record_id, "done")
            await db.log_event("analysis_done", issue_id=record_id, username=author,
                               duration_ms=_duration_ms, detail={"rule_type": result.rule_type, "confidence": str(result.confidence)})

            # Post success comment with formatted result
            comment_body = format_analysis_comment(result.model_dump(), identifier, primary_language=issue_language, author=author)
            await client.create_comment(linear_issue_id, comment_body)

        logger.info(
            "Linear analysis complete: issue=%s task=%s type=%s confidence=%s failure=%s",
            identifier, task_id, result.problem_type, result.confidence, is_failure,
        )

    except Exception as e:
        logger.error("Linear analysis failed for issue %s: %s", linear_issue_id, e, exc_info=True)
        # Try to post error comment
        try:
            await client.create_comment(
                linear_issue_id,
                f"🤖 **AI analysis encountered an error**:\n\n```\n{str(e)[:500]}\n```\n\nPlease check the Jarvis logs.",
            )
        except Exception:
            logger.error("Failed to post error comment to Linear")

    finally:
        _active_issues.discard(linear_issue_id)
        try:
            await client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_field(text: str, keywords: list[str]) -> str:
    """Best-effort extract a field value from text by looking for keyword patterns."""
    import re
    for kw in keywords:
        pattern = rf"{re.escape(kw)}\s*[:：=]\s*(\S+)"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _detect_language(text: str) -> str:
    """Detect whether text is Chinese or English. Returns 'zh' or 'en'."""
    if not text:
        return "en"
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    return "zh" if chinese_chars > 0 else "en"


def _extract_followup_question(body: str) -> str:
    """Extract the followup question from a comment body.

    Strips the trigger keyword and any leading/trailing whitespace.
    Returns a default message if nothing remains after the trigger.
    """
    idx = body.lower().find(_FOLLOWUP_TRIGGER)
    if idx == -1:
        return ""
    question = body[idx + len(_FOLLOWUP_TRIGGER):].strip()
    return question or "请进一步分析这个问题"
