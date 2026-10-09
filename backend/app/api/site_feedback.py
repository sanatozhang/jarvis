"""Global site feedback widget → Feishu DM to admin."""
from __future__ import annotations

import base64
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.config import get_settings
from app.services import system_notify

logger = logging.getLogger("jarvis.api.site_feedback")
router = APIRouter()


class SiteFeedbackInput(BaseModel):
    message: str
    page_url: str | None = None
    screenshot: str | None = None   # data:image/png;base64,...
    user_email: str | None = None

    @field_validator("message")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message required")
        return v.strip()


def _decode_screenshot(raw: str) -> bytes | None:
    if not raw:
        return None
    b64 = raw.split(",", 1)[1] if raw.startswith("data:") else raw
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


@router.post("")
async def submit_site_feedback(req: SiteFeedbackInput):
    recipient = get_settings().feedback_recipient
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = ["📝 站点反馈", f"内容：{req.message}"]
    if req.user_email:
        lines.append(f"提交人：{req.user_email}")
    if req.page_url:
        lines.append(f"工单页：{req.page_url}")
    lines.append(f"时间：{ts}")
    text = "\n".join(lines)
    # Slack 一律英文（2026-10-09）：标签全英文；反馈内容是用户原文，照发不翻译
    lines_en = ["📝 Site feedback", f"Message: {req.message}"]
    if req.user_email:
        lines_en.append(f"Submitted by: {req.user_email}")
    if req.page_url:
        lines_en.append(f"Page: {req.page_url}")
    lines_en.append(f"Time: {ts}")
    text_en = "\n".join(lines_en)

    text_ok = await system_notify.send_text(recipient, text, text_en=text_en)

    image_sent = False
    img_bytes = _decode_screenshot(req.screenshot) if req.screenshot else None
    if img_bytes:
        image_sent = await system_notify.send_image(recipient, img_bytes, "feedback-screenshot.png")
        if not image_sent:
            logger.warning("Feedback screenshot delivery failed")

    if not text_ok:
        raise HTTPException(status_code=502,
                            detail=f"Failed to deliver feedback via {system_notify.provider()}")
    return {"status": "sent", "image_sent": image_sent}
