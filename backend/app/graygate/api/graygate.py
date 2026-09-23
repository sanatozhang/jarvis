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
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.db import database as db
from app.graygate.config import get_graygate_settings
from app.graygate.services import notify
from app.graygate.services.card_builder import collect_report_data
from app.graygate.services.focus_version import (
    clear_focus_version,
    get_all_focus_versions,
    get_focus_version_audit_history,
    set_focus_version,
)

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

    settings = get_graygate_settings()
    provider = (settings.notify_provider or "feishu").strip().lower()

    # 预览按**当前 provider** 渲染，不是恒渲染飞书卡片：这个端点的用途是
    # "发之前先看一眼要发什么"，切到 Slack 之后还给飞书 card 就失去意义了。
    # 取数只跑一次（collect_report_data），两个渲染器消费同一份数据。
    data = await collect_report_data(resolved_date)

    result: Dict[str, Any] = {
        "target_date": resolved_date.isoformat(),
        "available": data is not None,
        "provider": provider,
        # `card` 保持原字段名和语义（飞书 card）；provider=slack 时为空，
        # 预览看 `blocks`。前端的 GraygateTriggerResult 里 card 本来就是可选的。
        "card": {},
        "blocks": [],
        "dry_run": dry_run,
        "sent": False,
        "reason": "",
    }
    if data is not None:
        if provider == "slack":
            from app.graygate.services.slack_report import assemble_slack_message

            msg = assemble_slack_message(data)
            result["blocks"] = msg.payload
            result["thread_folds"] = [f.title for f in msg.folds]
        else:
            from app.graygate.services.card_builder import assemble_feishu_card

            result["card"] = assemble_feishu_card(data)

    # dry_run=True（默认）→ 只预览，绝不真发。
    if dry_run:
        return result

    # send_enabled（历史名 feishu_enabled）是总闸——手动触发不能绕过它，
    # 即使显式传了 dry_run=false。
    if not settings.send_enabled:
        result["reason"] = "send_enabled=False"
        return result

    if data is None:
        # 没有数据可报（两平台版本枚举都是空），没有内容值得发送。
        result["reason"] = "available=False"
        return result

    sent = await notify.send_daily_report(resolved_date)
    result["sent"] = bool(sent)
    if not sent:
        result["reason"] = "send_failed"
    return result


class FocusVersionPatch(BaseModel):
    platform: str    # "ios" / "android"
    version: str = ""  # 空字符串 = 清空人工指定，回落到 session 数自动判定


@router.get("/focus-version")
async def get_focus_version_endpoint() -> dict:
    """查询当前人工指定的"主要版本"（未设置的平台返回 null，报告里会回落到
    session 数自动判定）。"""
    return await get_all_focus_versions()


def _resolve_caller(request: Request, x_api_key: Optional[str]) -> str:
    """解析这次写请求的调用方身份，没有合法身份直接拒绝。

    2026-09-16：102 上实测发现有不明调用方持续把 focus-version 摁回一个值，
    因为写路径完全不鉴权、任何人都能匿名调用——changed_by 记成 "aeolus" 只是
    "记录了变化"，没解决"谁都能改"这个根问题。改成：
    - SSO 登录态（浏览器 /settings 页面）→ 用邮箱识别，不需要额外带 key；
    - 没有登录态 → 必须在 `X-Graygate-Api-Key` header 带上配置好的密钥之一
      （`graygate_api_key_jarvis` / `graygate_api_key_runway`，每个调用方一把，
      互不相同），命中哪把就记为哪个调用方；一把都不匹配直接 401。
    """
    user = getattr(request.state, "user", None) or {}
    email = user.get("email") or user.get("username")
    if email:
        return email

    s = get_graygate_settings()
    key_map = {
        s.api_key_jarvis: "jarvis",
        s.api_key_runway: "runway",
    }
    key_map.pop("", None)  # 未配置的密钥是空字符串，不能被空 header 命中
    caller = key_map.get(x_api_key or "")
    if not caller:
        raise HTTPException(status_code=401, detail="missing or invalid X-Graygate-Api-Key")
    return caller


@router.post("/focus-version")
async def set_focus_version_endpoint(
    body: FocusVersionPatch,
    request: Request,
    x_graygate_api_key: Optional[str] = Header(None, alias="X-Graygate-Api-Key"),
) -> dict:
    """设置/清空某平台人工指定的"主要版本"——发布新版本时用这个接口告诉系统
    "现在关注这个 build"，不用等它自然爬到 session 数第一。

    传空字符串 `version` 清空指定，回落到 session 数自动判定的 top_version。

    2026-09-15/16：每次变更都写审计（谁/何时/旧值→新值，落 DB 不随重启消失，
    同时打一行 INFO 日志方便 `docker compose logs` 直接 grep）+ 飞书通知到
    4.0灰度数据跟进群（见 focus_version.py::_record_and_notify）。调用方鉴权
    见 `_resolve_caller`：SSO 登录态用邮箱，否则必须带合法的 API key，两者都
    没有直接 401——不再允许匿名调用。
    """
    if body.platform not in ("ios", "android"):
        raise HTTPException(status_code=400, detail="platform must be 'ios' or 'android'")
    changed_by = _resolve_caller(request, x_graygate_api_key)
    if body.version:
        await set_focus_version(body.platform, body.version, changed_by=changed_by)
    else:
        await clear_focus_version(body.platform, changed_by=changed_by)
    return await get_all_focus_versions()


@router.get("/focus-version/history")
async def get_focus_version_history_endpoint(
    platform: Optional[str] = Query(None, description="ios / android，不传返回全部"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """查询"主要版本"变更审计（谁/何时/旧值→新值），只读，不受任何 kill switch 约束。"""
    if platform and platform not in ("ios", "android"):
        raise HTTPException(status_code=400, detail="platform must be 'ios' or 'android'")
    return {"items": await get_focus_version_audit_history(platform or "", limit)}
