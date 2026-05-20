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
