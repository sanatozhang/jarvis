"""Unified entry for sending Feishu messages BY USERNAME.

Existing callers either had emails on hand (oncall) or passed empty strings
when feishu_email was missing — causing ValueError surprises. This module
resolves username → user → feishu_email and gracefully skips unsendable
recipients, returning a structured result for caller-side logging.

Use this for any 'notify these humans' flow.
Use feishu_cli.send_message directly only when you already have a verified
email or chat_id (e.g. oncall list).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.db import database as db
from app.services.feishu_cli import send_message as feishu_send_message


logger = logging.getLogger("jarvis.notify")


@dataclass
class NotifyResult:
    sent:    List[str]              = field(default_factory=list)
    skipped: List[Tuple[str, str]]  = field(default_factory=list)
    failed:  List[Tuple[str, str]]  = field(default_factory=list)


async def notify_users_by_username(
    *,
    usernames: List[str],
    text: str = "",
    card: Optional[dict] = None,
) -> NotifyResult:
    """Send a Feishu message to each username after resolving their email.

    - Unknown user → skipped with reason 'user_not_found'.
    - User without feishu_email → skipped with reason 'no_feishu_email'.
    - Send exception → captured in `failed` with the stringified error.

    Note: the underlying feishu_cli.send_message currently supports only
    text/markdown — `card` is accepted for future use but today routes through
    text fallback if no `text` is provided.
    """
    result = NotifyResult()
    for username in usernames:
        user = await db.get_user(username)
        if not user:
            result.skipped.append((username, "user_not_found"))
            logger.info("notify_skipped username=%s reason=user_not_found", username)
            continue
        email = user.get("feishu_email") or ""
        if not email:
            result.skipped.append((username, "no_feishu_email"))
            logger.info("notify_skipped username=%s reason=no_feishu_email", username)
            continue
        try:
            body_text = text if text else (str(card) if card is not None else "")
            await feishu_send_message(email=email, text=body_text)
            result.sent.append(username)
        except Exception as e:
            result.failed.append((username, str(e)))
            logger.error("feishu_send_failed username=%s err=%s", username, e)
    return result


async def notify_issue_creator_on_complete(
    *,
    issue_id: str,
    task_id: str,
    status: str,
) -> Optional[NotifyResult]:
    """Notify the issue creator (in English) when analysis finishes (done OR failed).

    No-op when the issue has no `created_by` (Linear webhook flow etc.) or the
    creator has no feishu_email on file.
    """
    issue = await db.get_issue(issue_id)
    if not issue:
        logger.info("notify_creator_skipped issue=%s reason=issue_not_found", issue_id)
        return None
    creator = (issue.get("created_by") or "").strip()
    if not creator:
        logger.info("notify_creator_skipped issue=%s reason=no_creator", issue_id)
        return None

    from app.config import get_settings
    settings = get_settings()
    base_url = (getattr(settings, "frontend_base_url", "") or "").rstrip("/")
    detail_url = f"{base_url}/?detail={issue_id}" if base_url else f"/?detail={issue_id}"

    desc = (issue.get("description") or "").strip()
    desc_short = desc[:300] + ("…" if len(desc) > 300 else "")
    verb = "completed successfully" if status == "done" else "failed"

    text = (
        f"✓ Your ticket analysis has {verb}.\n\n"
        f"Ticket ID: {issue_id}\n"
        f"Description: {desc_short or '(no description)'}\n"
        f"URL: {detail_url}\n\n"
        f"Please review with the ticket description and URL above."
    )
    return await notify_users_by_username(usernames=[creator], text=text)
