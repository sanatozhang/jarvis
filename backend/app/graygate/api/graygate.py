"""手动触发 4.0.3 灰度日报（admin only）。

写法参照 `app/api/oncall.py` 里 `POST /weekly-greeting`（`trigger_weekly_greeting`）：
admin 鉴权 + `dry_run` 默认安全闸。

这个端点**不受** `enabled` / `scheduler_enabled` kill switch 约束——手动触发就是
要能在开关关闭时也能验证（照抄 `oncall.py` `sync-from-feishu` 端点"这个端点本身
不受 xxx 开关约束"的注释风格）。但仍然**受** `feishu_enabled` 约束：这是"总闸"，
`dry_run=False` 也不能绕过它——`feishu_enabled=False` 时即使 `dry_run=False`，
也不真的发送，返回体里说明原因。

`target_date` 不传时用 BJT"昨天"，和调度器（`workers/scheduler.py`）同一套换算
逻辑（`now_bjt.date() - 1 天`），不写第二份。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from app.db import database as db
from app.graygate.config import get_graygate_settings
from app.graygate.services.report_builder import build_report

logger = logging.getLogger("jarvis.api.graygate")
router = APIRouter(prefix="/api/graygate", tags=["graygate"])

_BJT = ZoneInfo("Asia/Shanghai")


def _default_target_date() -> date:
    """BJT 昨天——与 `workers/scheduler.py` 的 `target_date` 换算逻辑完全一致。"""
    return datetime.now(_BJT).date() - timedelta(days=1)


@router.post("/trigger")
async def trigger_report(
    username: str = Query(..., description="Admin username"),
    dry_run: bool = Query(True, description="预览：只返回渲染好的 markdown，不发飞书"),
    target_date: Optional[str] = Query(None, description="ISO 日期(YYYY-MM-DD)，默认昨天(BJT)"),
):
    """手动触发一次 4.0.3 灰度日报（admin only）。dry_run 默认 True，是这个端点自己的安全闸。"""
    user = await db.get_user(username)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can trigger graygate report")

    if target_date:
        try:
            resolved_date = date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid target_date: {target_date}")
    else:
        resolved_date = _default_target_date()

    report = await build_report(resolved_date)

    result = {
        "target_date": resolved_date.isoformat(),
        "available": report.available,
        "markdown": report.markdown,
        "dry_run": dry_run,
        "sent": False,
        "reason": "",
    }

    # dry_run=True（默认）→ 只预览，绝不调用 send_message。
    if dry_run:
        return result

    settings = get_graygate_settings()

    # feishu_enabled 是总闸——手动触发不能绕过它，即使显式传了 dry_run=false。
    if not settings.feishu_enabled:
        result["reason"] = "feishu_enabled=False"
        return result

    if not report.available:
        # 没有数据可报（两平台版本枚举都是空），没有 markdown 内容值得发送。
        result["reason"] = "available=False"
        return result

    from app.services.feishu_cli import send_message

    sent = await send_message(chat_id=settings.feishu_chat_id, text=report.markdown)
    result["sent"] = sent
    if not sent:
        result["reason"] = "send_failed"
    return result
