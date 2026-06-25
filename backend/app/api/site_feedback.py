"""Global site feedback widget → Feishu DM to admin."""
from __future__ import annotations

import base64
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.config import get_settings
from app.services import feishu_cli

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

    text_ok = await feishu_cli.send_message(email=recipient, text=text)

    image_sent = False
    img_bytes = _decode_screenshot(req.screenshot) if req.screenshot else None
    if img_bytes:
        try:
            image_key = await feishu_cli.upload_image(img_bytes)
            image_sent = await feishu_cli.send_image_message(image_key=image_key, email=recipient)
        except Exception as e:
            logger.warning("Feedback screenshot delivery failed: %s", e)

    if not text_ok:
        raise HTTPException(status_code=502, detail="Failed to deliver feedback to Feishu")
    return {"status": "sent", "image_sent": image_sent}
