"""
Daily escalation reminder.

Each morning at 09:00 (Asia/Shanghai), scans escalations that:
  - status == "in_progress"
  - escalated_at older than 24h
  - haven't been reminded today

For each, post @-mention in the Feishu group + DM the oncall engineer with
a link to the oncall page (so they don't forget pending tickets).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import os

from app.db import database as db

logger = logging.getLogger("jarvis.escalation_reminder")

# Reminder fires once per day at this hour (Asia/Shanghai).
REMINDER_HOUR_LOCAL = 9
SHANGHAI_TZ = timezone(timedelta(hours=8))


def _seconds_until_next_run(now_local: datetime) -> float:
    target = now_local.replace(hour=REMINDER_HOUR_LOCAL, minute=0, second=0, microsecond=0)
    if now_local >= target:
        target = target + timedelta(days=1)
    return (target - now_local).total_seconds()


def _appllo_base() -> str:
    return (os.environ.get("APPLLO_BASE_URL", "") or "").rstrip("/")


async def _scan_and_remind() -> int:
    """Send reminders for stale escalations. Returns number reminded."""
    from app.services.feishu_cli import _emails_to_open_id_map, send_message

    cutoff_age = timedelta(days=1)
    now = datetime.utcnow()

    async with db.get_session() as session:
        stmt = select(db.IssueRecord).where(
            db.IssueRecord.escalated_at.isnot(None),
            db.IssueRecord.escalation_status == "in_progress",
            db.IssueRecord.deleted == False,
        )
        result = await session.execute(stmt)
        records = result.scalars().all()

    today_local = datetime.now(SHANGHAI_TZ).date()
    candidates = []
    for r in records:
        if not r.escalated_at:
            continue
        if (now - r.escalated_at) < cutoff_age:
            continue  # less than a day old, give them today to handle
        if r.escalation_reminded_at and r.escalation_reminded_at.date() == today_local:
            continue  # already pinged today
        candidates.append(r)

    if not candidates:
        logger.info("No stale escalations to remind today")
        return 0

    # Resolve oncall once for all reminders today
    oncall_emails = await db.get_current_oncall()
    oncall_id_map = await _emails_to_open_id_map(oncall_emails) if oncall_emails else {}

    base = _appllo_base()
    oncall_page_url = f"{base}/oncall" if base else ""

    reminded = 0
    for r in candidates:
        issue_id = r.id
        description = (r.description or issue_id)[:200]
        try:
            problem_type = ""
            analysis = await db.get_analysis_by_issue(issue_id)
            if analysis:
                problem_type = analysis.problem_type or ""

            age_hours = int((now - r.escalated_at).total_seconds() // 3600)
            ticket_link = f"{base}/tracking?detail={issue_id}" if base else ""

            # 1) Post @-mention in the group chat
            if r.escalation_chat_id and oncall_id_map:
                at_tags = " ".join(
                    f'<at user_id="{oid}">{email.split("@")[0]}</at>'
                    for email, oid in oncall_id_map.items()
                )
                lines = [
                    "⏰ 工单跟进提醒",
                    f"该工单已转交 {age_hours} 小时，仍未标记解决。",
                    f"问题: {description}",
                ]
                if problem_type:
                    lines.append(f"分类: {problem_type}")
                if ticket_link:
                    lines.append(f"详情: {ticket_link}")
                lines.append(f"\n{at_tags} 请关注并尽快推进，处理完成后在 Appllo 标记完成。")
                try:
                    await send_message(chat_id=r.escalation_chat_id, text="\n".join(lines))
                    logger.info("Group reminder sent for %s (chat %s, %dh old)", issue_id, r.escalation_chat_id, age_hours)
                except Exception as e:
                    logger.warning("Failed to send group reminder for %s: %s", issue_id, e)

            # 2) DM each oncall engineer (separately) — guides them to oncall page
            dm_lines = [
                "🔔 你有未解决的转交工单",
                f"工单: {description}",
                f"已转交 {age_hours} 小时未关闭。",
            ]
            if oncall_page_url:
                dm_lines.append(f"在 Appllo 查看待办列表: {oncall_page_url}")
            if ticket_link:
                dm_lines.append(f"工单详情: {ticket_link}")
            dm_text = "\n".join(dm_lines)

            for email in oncall_emails:
                try:
                    await send_message(email=email, text=dm_text)
                except Exception as e:
                    logger.warning("Failed to DM %s about %s: %s", email, issue_id, e)

            await db.mark_escalation_reminded(issue_id)
            await db.log_event(
                "escalation_reminder",
                issue_id=issue_id,
                detail={"age_hours": age_hours, "chat_id": r.escalation_chat_id or "", "oncall": oncall_emails},
            )
            reminded += 1
        except Exception as e:
            logger.error("Reminder failed for %s: %s", issue_id, e, exc_info=True)

    logger.info("Escalation reminder run complete: %d reminded out of %d candidates", reminded, len(candidates))
    return reminded


async def escalation_reminder_loop():
    """Background loop: fire daily at 09:00 Shanghai time."""
    while True:
        try:
            now_local = datetime.now(SHANGHAI_TZ)
            wait_s = _seconds_until_next_run(now_local)
            logger.info("Next escalation reminder in %.1f hours (at 09:00 SH)", wait_s / 3600)
            await asyncio.sleep(wait_s)
            await _scan_and_remind()
            # Avoid double-fire if scan finished sub-second
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("Escalation reminder loop cancelled")
            return
        except Exception as e:
            logger.error("Escalation reminder loop error (retry in 1h): %s", e, exc_info=True)
            await asyncio.sleep(3600)
