"""
Crashguard 早晚报调度。

简化策略：每 60 秒 tick 一次，解析 morning_cron / evening_cron 的 "M H * * *"。
当前小时分钟匹配 → 触发对应 report；用 last_fired_at 防同一分钟重发。
不引入 apscheduler/croniter 依赖，保留模块独立性。

支持的 cron 形式：
- "M H * * *"           — 固定时刻
- "*/N * * * *"         — 每 N 分钟（仅 minute 字段）
- "M */N * * *"         — 每 N 小时的第 M 分钟
其他复杂表达式不支持，直接 skip。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Tuple

logger = logging.getLogger("crashguard.scheduler")

_TICK_INTERVAL_SEC = 60
_last_fired: dict[str, str] = {}  # report_type → "YYYY-MM-DD HH:MM"
_pr_sync_last_fired: str = ""    # "YYYY-MM-DD HH:MM" 防同分钟重跑


def _cron_matches(expr: str, now: datetime) -> bool:
    """解析极简 cron。匹配返回 True。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    if dom_f != "*" or month_f != "*" or dow_f != "*":
        return False  # 仅支持每天

    def field_match(field: str, value: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            try:
                step = int(field[2:])
                return step > 0 and value % step == 0
            except ValueError:
                return False
        try:
            return int(field) == value
        except ValueError:
            return False

    return field_match(minute_f, now.minute) and field_match(hour_f, now.hour)


async def _tick_once() -> None:
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.services.daily_report import send_daily_report

    s = get_crashguard_settings()
    if not s.enabled or not s.feishu_enabled:
        return
    # 多实例部署兜底：只有显式开启 scheduler 的机器跑 cron 触发；
    # 即使两台都开了，下游 send_daily_report 也有 DB 抢锁去重保护。
    if not getattr(s, "scheduler_enabled", True):
        return
    if not s.feishu_target_chat_id:
        return

    now = datetime.now()
    tag = now.strftime("%Y-%m-%d %H:%M")
    schedule = (
        ("morning", s.morning_cron),
        ("evening", s.evening_cron),
    )
    for report_type, cron_expr in schedule:
        if _last_fired.get(report_type) == tag:
            continue
        if not _cron_matches(cron_expr, now):
            continue
        try:
            res = await send_daily_report(report_type, target_date=now.date())
            _last_fired[report_type] = tag
            logger.info(
                "crashguard daily_report fired: type=%s ok=%s sent=%s reason=%s",
                report_type, res.get("ok"), res.get("sent"), res.get("skipped_reason"),
            )
        except Exception:
            logger.exception("crashguard daily_report tick failed: type=%s", report_type)

    # PR 状态同步（独立 cron，默认 */15）
    global _pr_sync_last_fired
    pr_cron = getattr(s, "pr_sync_cron", "") or ""
    if pr_cron and _pr_sync_last_fired != tag and _cron_matches(pr_cron, now):
        try:
            from app.crashguard.services.pr_sync import sync_all_open_prs
            res = await sync_all_open_prs()
            _pr_sync_last_fired = tag
            logger.info(
                "crashguard pr_sync fired: checked=%d changed=%d errors=%d",
                res.get("checked", 0), res.get("changed", 0), res.get("errors", 0),
            )
        except Exception:
            logger.exception("crashguard pr_sync tick failed")


async def report_scheduler_loop() -> None:
    """主循环。每 60 秒 tick 一次。"""
    logger.info("crashguard report_scheduler_loop started")
    while True:
        try:
            await _tick_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("crashguard scheduler tick error (continuing)")
        try:
            await asyncio.sleep(_TICK_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
