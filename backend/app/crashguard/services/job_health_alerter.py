"""
定时任务健康度兜底告警。

底层逻辑：cron tick 静默失败是最大可观测性盲点。本任务每 5 分钟扫一遍 heartbeat 表，
- 拉每个已知 job 的最新心跳 + 最近 50 次失败计数 + 上次成功时间
- 用与 `/api/crash/jobs/status` 一致的判定：stale / failing / degraded / ok
- 任一任务 health ∈ (failing, stale) 且距上次告警 > cooldown_minutes → 聚合一张飞书卡片
- 告警节流：进程级 _last_alerted_at dict 防 30min 内重复刷屏；重启后允许重发一次（可接受）

返回 dict 给 scheduler logging + 心跳 summary。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashJobHeartbeat
from app.db.database import get_session

logger = logging.getLogger("crashguard.job_health_alerter")


# 进程级节流：job_name → 上次发告警的 UTC 时间
_last_alerted_at: Dict[str, datetime] = {}


def _interval_minutes_from_cron(cron_expr: str) -> Optional[int]:
    """与 api/crash.py 同名函数对齐——简版独立实现避免跨层 import。"""
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, *_ = parts
    if minute_f.startswith("*/"):
        try:
            return max(1, int(minute_f[2:]))
        except ValueError:
            return None
    if hour_f.startswith("*/"):
        try:
            return max(1, int(hour_f[2:])) * 60
        except ValueError:
            return None
    if minute_f != "*" and hour_f != "*":
        return 24 * 60
    return None


async def run_job_health_check() -> Dict[str, Any]:
    """单次扫描；返回 logging dict（也写进 heartbeat summary）。"""
    s = get_crashguard_settings()
    if not s.enabled or not s.feishu_enabled:
        return {"skipped": "kill_switch"}
    if not getattr(s, "job_health_alert_enabled", True):
        return {"skipped": "job_health_alert_disabled"}

    # 与 _JOB_META 保持对齐——但本服务避免反向 import api 层，重新声明一份精简元数据
    job_meta: List[Dict[str, str]] = [
        {"name": "core_metric", "cron_field": "core_metric_cron"},
        {"name": "hourly_alert", "cron_field": "hourly_alert_cron"},
        {"name": "analyze_tick", "cron_field": "analyze_cron"},
        {"name": "pr_sync", "cron_field": "pr_sync_cron"},
        {"name": "pipeline", "cron_field": "pipeline_cron"},
        {"name": "morning_daily", "cron_field": "morning_cron"},
        {"name": "evening_daily", "cron_field": "evening_cron"},
    ]

    now_utc = datetime.utcnow()
    cooldown_min = int(getattr(s, "job_health_alert_cooldown_minutes", 30) or 30)
    cooldown = timedelta(minutes=cooldown_min)

    unhealthy: List[Dict[str, Any]] = []

    async with get_session() as session:
        for meta in job_meta:
            jn = meta["name"]
            cron_expr = getattr(s, meta["cron_field"], "") or ""
            interval = _interval_minutes_from_cron(cron_expr)

            # 上次心跳
            last_row = (await session.execute(
                select(CrashJobHeartbeat)
                .where(CrashJobHeartbeat.job_name == jn)
                .order_by(desc(CrashJobHeartbeat.fired_at))
                .limit(1)
            )).scalars().first()
            # 上次成功
            last_success_row = (await session.execute(
                select(CrashJobHeartbeat)
                .where(
                    CrashJobHeartbeat.job_name == jn,
                    CrashJobHeartbeat.status == "success",
                )
                .order_by(desc(CrashJobHeartbeat.fired_at))
                .limit(1)
            )).scalars().first()
            # 近 50 次连续失败
            recent = (await session.execute(
                select(CrashJobHeartbeat)
                .where(CrashJobHeartbeat.job_name == jn)
                .order_by(desc(CrashJobHeartbeat.fired_at))
                .limit(50)
            )).scalars().all()
            consecutive_failures = 0
            for r in recent:
                if r.status == "failed":
                    consecutive_failures += 1
                else:
                    break

            stale = False
            if interval and last_success_row is not None and last_success_row.fired_at:
                age_min = (now_utc - last_success_row.fired_at).total_seconds() / 60.0
                if age_min > 2 * interval:
                    stale = True

            health: str
            if stale:
                health = "stale"
            elif consecutive_failures >= 3:
                health = "failing"
            else:
                health = "ok"

            if health == "ok":
                continue

            # 节流：距上次告警 < cooldown 跳过
            last_alert_at = _last_alerted_at.get(jn)
            if last_alert_at is not None and (now_utc - last_alert_at) < cooldown:
                continue

            unhealthy.append({
                "job_name": jn,
                "health": health,
                "consecutive_failures": consecutive_failures,
                "last_status": last_row.status if last_row else None,
                "last_fired_at": last_row.fired_at.isoformat() if last_row and last_row.fired_at else None,
                "last_success_at": (
                    last_success_row.fired_at.isoformat()
                    if last_success_row and last_success_row.fired_at else None
                ),
                "last_error": ((last_row.error or "") if last_row else "")[:200],
                "interval_minutes": interval,
            })

    if not unhealthy:
        return {"ok": True, "alerted": False, "scanned": len(job_meta)}

    # 发送聚合飞书卡片
    from app.crashguard.services.feishu_card import build_job_health_alert_card
    card = build_job_health_alert_card(
        items=unhealthy,
        cooldown_minutes=cooldown_min,
        frontend_base_url=s.frontend_base_url,
    )
    sent_ok = False
    try:
        from app.services.feishu_cli import send_interactive_card
        if s.feishu_target_chat_id:
            sent_ok = await send_interactive_card(chat_id=s.feishu_target_chat_id, card=card)
        elif s.feishu_target_email:
            sent_ok = await send_interactive_card(email=s.feishu_target_email, card=card)
    except Exception:
        logger.exception("job_health_alerter: feishu send error")

    # 记节流戳（即使发送失败也算告警尝试，避免一直刷屏）
    for it in unhealthy:
        _last_alerted_at[it["job_name"]] = now_utc

    logger.info(
        "job_health_alert fired: jobs=%s sent=%s",
        [it["job_name"] for it in unhealthy], sent_ok,
    )
    return {
        "ok": True,
        "alerted": True,
        "sent": sent_ok,
        "unhealthy_count": len(unhealthy),
        "unhealthy_jobs": [it["job_name"] for it in unhealthy],
    }
