"""Coreguard 周期调度（极简 cron，60s tick）。

底层逻辑：与 crashguard 隔离，独立 scheduler。
当前 demo 阶段只挂 1 个 job：
  - coreguard_hourly_watch  cron=`5 * * * *`  每小时第 5 分钟跑 22 指标 SHoW 对比

未来扩展（design §7）：
  - coreguard_daily_p2_report  `0 8 * * *`
  - coreguard_lifecycle_tick   `*/15 * * * *`
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from app.coreguard.config import get_coreguard_settings
from app.db.database import get_session

logger = logging.getLogger("coreguard.scheduler")

_TICK_INTERVAL_SEC = 60
_last_fired_hourly: str = ""   # "YYYY-MM-DD HH:MM"  进程级幂等


def _cron_matches(expr: str, now: datetime) -> bool:
    """极简 cron 解析（M H * * * 或 */N * * * *）。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    if dom_f != "*" or month_f != "*" or dow_f != "*":
        return False

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


async def _write_heartbeat(job_name: str, status: str, duration_ms: int,
                            summary: dict, error: Optional[str] = None) -> None:
    """写 coreguard_job_heartbeats 表（同 crashguard 模式）。"""
    try:
        from app.coreguard.models import CoreguardJobHeartbeat
        async with get_session() as session:
            session.add(CoreguardJobHeartbeat(
                job_name=job_name,
                fired_at=datetime.utcnow(),
                status=status,
                duration_ms=duration_ms,
                summary=json.dumps(summary, ensure_ascii=False)[:2000],
                error=(error or "")[:1000],
            ))
            await session.commit()
    except Exception as e:
        logger.warning("heartbeat write failed: %s", e)


async def _run_hourly_watch_once() -> None:
    """跑一次 hourly_watch（含写心跳）。"""
    start = time.monotonic()
    status = "ok"
    error = None
    summary: dict = {}
    try:
        from app.coreguard.services.metric_watcher import run_all
        # cron 模式：dry_run=False（真发飞书），force_alert=False（无异常不打扰）
        result = await run_all(dry_run=False, force_alert=False)
        summary = {
            "evaluated": result.get("evaluated"),
            "breached": result.get("breached"),
            "healthy": result.get("healthy"),
            "errored": result.get("errored"),
            "alert_sent": result.get("alert_sent"),
        }
        if result.get("errored", 0) > 0:
            status = "partial"
    except Exception as e:
        status = "failed"
        error = repr(e)[:500]
        logger.exception("hourly_watch failed")
    duration_ms = int((time.monotonic() - start) * 1000)
    await _write_heartbeat("coreguard_hourly_watch", status, duration_ms, summary, error)
    logger.info("coreguard_hourly_watch done: status=%s duration_ms=%d summary=%s",
                status, duration_ms, summary)


async def _tick_once() -> None:
    """一次 tick：检查 cron 是否匹配，匹配则触发对应 job。"""
    global _last_fired_hourly

    s = get_coreguard_settings()
    if not s.enabled:
        return
    if not s.scheduler_enabled:
        return

    now = datetime.utcnow()
    minute_key = now.strftime("%Y-%m-%d %H:%M")

    # job: coreguard_hourly_watch
    if _cron_matches(s.hourly_watch_cron, now):
        if minute_key != _last_fired_hourly:
            _last_fired_hourly = minute_key
            logger.info("coreguard_hourly_watch fired at %s (cron=%s)", minute_key, s.hourly_watch_cron)
            asyncio.create_task(_run_hourly_watch_once())


async def scheduler_loop() -> None:
    """周期 60s tick，进程级幂等防同分钟重复触发。"""
    logger.info("coreguard scheduler starting (interval=%ds)", _TICK_INTERVAL_SEC)
    # 启动后先等一个 tick 避免与启动其他任务冲突
    await asyncio.sleep(5)
    while True:
        try:
            await _tick_once()
        except Exception as e:
            logger.exception("coreguard scheduler tick error: %s", e)
        await asyncio.sleep(_TICK_INTERVAL_SEC)
