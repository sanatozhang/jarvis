"""
Notification service — sends alerts to oncall engineers.

Currently supports Feishu. Extensible to Slack, email, etc.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import get_settings
from app.db import database as db

logger = logging.getLogger("jarvis.notify")


# ---------------------------------------------------------------------------
# Abstract notifier interface
# ---------------------------------------------------------------------------
class BaseNotifier:
    async def send(self, recipients: List[str], message: Dict[str, Any]) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Feishu notifier — sends message via Feishu bot to users by email
# ---------------------------------------------------------------------------
class FeishuNotifier(BaseNotifier):
    """Send Feishu messages using the bot's tenant token."""

    SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

    async def _get_user_id_by_email(self, http: httpx.AsyncClient, token: str, email: str) -> Optional[str]:
        """Lookup Feishu user_id by email."""
        url = f"https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id"
        resp = await http.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"emails": [email]},
        )
        data = resp.json()
        users = data.get("data", {}).get("user_list", [])
        if users and users[0].get("user_id"):
            return users[0]["user_id"]
        return None

    async def send(self, recipients: List[str], message: Dict[str, Any]) -> bool:
        """Send a Feishu message card to each recipient (by email directly)."""
        settings = get_settings()
        if not settings.feishu.app_id:
            logger.warning("Feishu not configured, skip notification")
            return False

        async with httpx.AsyncClient(verify=False, timeout=30) as http:
            # Get token
            token_resp = await http.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/",
                json={"app_id": settings.feishu.app_id, "app_secret": settings.feishu.app_secret},
            )
            token = token_resp.json().get("tenant_access_token", "")
            if not token:
                logger.error("Failed to get Feishu token for notification")
                return False

            card = _build_feishu_card(message)
            sent = 0

            for email in recipients:
                # Send directly by email — no need to lookup user_id first
                resp = await http.post(
                    f"{self.SEND_URL}?receive_id_type=email",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": email,
                        "msg_type": "interactive",
                        "content": card,
                    },
                )
                result = resp.json()
                if result.get("code") == 0:
                    sent += 1
                    logger.info("Feishu notification sent to %s", email)
                else:
                    logger.error("Feishu send to %s failed (code=%s): %s", email, result.get("code"), result.get("msg"))

            return sent > 0


# ---------------------------------------------------------------------------
# Slack notifier (placeholder for future)
# ---------------------------------------------------------------------------
class SlackNotifier(BaseNotifier):
    async def send(self, recipients: List[str], message: Dict[str, Any]) -> bool:
        # TODO: implement Slack webhook
        logger.info("SlackNotifier.send called (not implemented)")
        return False


# ---------------------------------------------------------------------------
# Feishu message card builder
# ---------------------------------------------------------------------------
def _build_feishu_card(msg: Dict[str, Any]) -> str:
    """Build a Feishu interactive card JSON string."""
    import json
    title = msg.get("title", "🔔 工单需要工程师处理")
    issue_id = msg.get("issue_id", "")
    description = msg.get("description", "")[:200]
    reason = msg.get("reason", "")
    zendesk = msg.get("zendesk_id", "")
    link = msg.get("link", "")

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**工单**: {issue_id}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**问题**: {description}"}},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**原因**: {reason}"}},
    ]
    if zendesk:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**Zendesk**: {zendesk}"}})
    if link:
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "查看详情"}, "url": link, "type": "primary"}
        ]})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
        "elements": elements,
    }
    return json.dumps(card, ensure_ascii=False)


# ---------------------------------------------------------------------------
# High-level: notify current oncall
# ---------------------------------------------------------------------------
async def notify_oncall(
    issue_id: str,
    description: str,
    reason: str,
    zendesk_id: str = "",
    link: str = "",
) -> bool:
    """Send notification to current oncall engineers."""
    recipients = await db.get_current_oncall()
    if not recipients:
        logger.warning("No oncall members configured, cannot send notification")
        return False

    message = {
        "title": "🔔 工单需要工程师处理",
        "issue_id": issue_id,
        "description": description,
        "reason": reason,
        "zendesk_id": zendesk_id,
        "link": link,
    }

    notifier = FeishuNotifier()
    return await notifier.send(recipients, message)
