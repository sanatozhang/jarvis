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

    logger.info("Linear webhook received: type=%s action=%s", event_type, action)

    data = payload.get("data", {})

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
    if trigger not in body.lower():
        logger.debug("Comment does not contain trigger keyword '%s', skipping", trigger)
        return

    # Extract issue ID from the webhook payload
    issue_id = comment_data.get("issueId") or comment_data.get("issue", {}).get("id", "")
    comment_id = comment_data.get("id", "")

    if not issue_id:
        logger.warning("Comment webhook missing issueId, skipping")
        return

    # Avoid duplicate triggers on the same issue
    if issue_id in _active_issues:
        logger.info("Analysis already in progress for issue %s, skipping", issue_id)
        return

    _active_issues.add(issue_id)

    logger.info(
        "Trigger detected! issue=%s comment=%s body_preview='%s'",
        issue_id, comment_id, body[:80],
    )

    # Launch analysis in background
    background_tasks.add_task(
        _run_linear_analysis,
        linear_issue_id=issue_id,
        trigger_comment_id=comment_id,
        trigger_body=body,
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

    logger.info(
        "Trigger detected in issue! issue=%s title='%s'",
        issue_id, title[:80],
    )

    background_tasks.add_task(
        _run_linear_analysis,
        linear_issue_id=issue_id,
        trigger_comment_id="",
        trigger_body=clean_description,
    )


async def _run_linear_analysis(
    linear_issue_id: str,
    trigger_comment_id: str,
    trigger_body: str,
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

        # Post acknowledgement comment
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
        await db.upsert_issue(issue.model_dump(), status="analyzing")
        await db.create_task(task_id=task_id, issue_id=record_id)
        await db.update_task(task_id, status="analyzing", progress=10, message="获取 Linear 工单信息...")

        # --- Step 3: Download attachments ---
        workspace = Path(settings.storage.workspace_dir) / task_id
        raw_dir = workspace / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        attachments = linear_issue.get("attachments", {}).get("nodes", [])
        downloaded_files = []

        for att in attachments:
            att_url = att.get("url", "")
            att_title = att.get("title", "") or "attachment"
            if not att_url:
                continue
            # Determine filename
            filename = att_title
            if "/" in att_url:
                url_filename = att_url.rsplit("/", 1)[-1].split("?")[0]
                if "." in url_filename:
                    filename = url_filename
            save_path = raw_dir / filename
            try:
                await client.download_attachment(att_url, str(save_path))
                downloaded_files.append(save_path)
            except Exception as e:
                logger.error("Failed to download attachment '%s': %s", att_title, e)

        await db.update_task(task_id, status="analyzing", progress=25, message=f"已下载 {len(downloaded_files)} 个附件")

        if not downloaded_files:
            # No attachments — still try to analyze based on description alone
            logger.warning("No downloadable attachments for issue %s, analyzing description only", identifier)

        # --- Step 4: Run analysis pipeline ---
        from app.services.decrypt import process_log_file
        from app.services.extractor import extract_for_rules
        from app.services.rule_engine import RuleEngine
        from app.services.agent_orchestrator import AgentOrchestrator
        from app.agents.base import BaseAgent

        # Decrypt / process log files
        log_paths = []
        processed_dir = workspace / "processed"
        processed_dir.mkdir(exist_ok=True)

        for fp in downloaded_files:
            log_path, incorrect, reason = process_log_file(fp, processed_dir)
            if log_path:
                log_paths.append(log_path)

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

        # Run agent
        prompt = BaseAgent.build_prompt(issue=issue, rules=rules, extraction=extraction)
        orchestrator = AgentOrchestrator()
        agent = orchestrator.select_agent(rule_type)
        result = await agent.analyze(workspace=workspace, prompt=prompt)
        result.task_id = task_id
        result.issue_id = record_id
        result.rule_type = rule_type

        # --- Step 5: Save result and post comment ---
        is_failure = (
            result.problem_type in ("分析超时", "日志解析失败", "Agent 不可用", "未知")
            or (result.confidence == "low" and result.needs_engineer and not result.user_reply)
            or "未产出结构化结果" in (result.root_cause or "")
        )

        await db.save_analysis(result.model_dump())

        if is_failure:
            await db.update_task(task_id, status="failed", progress=100, message="分析失败", error=result.root_cause[:200])
            await db.update_issue_status(record_id, "failed")

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

            # Post success comment with formatted result
            comment_body = format_analysis_comment(result.model_dump(), identifier)
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
        # Match patterns like "SN: ABC123" or "SN：ABC123" or "SN = ABC123"
        pattern = rf"{re.escape(kw)}\s*[:：=]\s*(\S+)"
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""
