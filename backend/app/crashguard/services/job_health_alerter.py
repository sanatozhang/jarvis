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
# 进程级"自动重试过"标记：job_name → 上次尝试自愈的 UTC 时间
# 抓手：发现 failing/stale 先尝试自动重跑一次，下 tick 仍失败才告警
_last_retried_at: Dict[str, datetime] = {}
# 连续自愈失败计数：job_name → 自愈触发后仍未产生 success 心跳的连续次数
# 抓手：用户语义"连续失败 3 次才告警"——单次 stale 自愈一次，3 次重试都没成功才推飞书
_consecutive_retry_failures: Dict[str, int] = {}
# 自愈成功告警阈值（连续 N 次自愈后仍无 success 心跳才升级为告警）
RETRY_FAILURE_THRESHOLD = 3


async def _try_auto_retry_job(job_name: str) -> tuple[bool, str]:
    """根据 job_name 派发到对应 runner，跑一次"自愈"重试。

    返回 (是否成功触发, summary)；触发本身报错则 summary 含 exception。
    不抛异常——本函数失败不阻断告警链路。

    长任务（pipeline）用 fire-and-forget asyncio.create_task，不阻塞 alerter loop；
    自愈结果由下次 health check 通过"是否产生新 success 心跳"事后判定。
    """
    import asyncio
    try:
        if job_name == "pr_sync":
            from app.crashguard.services.pr_sync import sync_all_open_prs
            r = await sync_all_open_prs()
            return True, f"pr_sync: checked={r.get('checked',0)} changed={r.get('changed',0)}"
        if job_name == "core_metric":
            from app.crashguard.services.core_metric_alerter import run_core_metric_tick
            r = await run_core_metric_tick()
            return True, f"core_metric: alerted={r.get('alerted')}"
        if job_name == "hourly_alert":
            from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
            r = await run_hourly_alert_tick()
            return True, f"hourly_alert: alerted={r.get('alerted')}"
        if job_name == "analyze_tick":
            from app.crashguard.workers.scheduler import _run_analyze_tick
            r = await _run_analyze_tick(max_per_tick=1)
            return True, f"analyze_tick: picked={r.get('picked',0)} done={r.get('completed',0)}"
        if job_name == "baseline_backfill":
            from app.crashguard.scripts_runtime import run_backfill_all
            r = await run_backfill_all(days_hourly=1, days_daily=1)
            return True, f"baseline_backfill: ok={r.get('ok')}"
        if job_name == "pipeline":
            # 长任务：fire-and-forget；同时主动写 pipeline 心跳
            from app.crashguard.workers.warmup import run_pipeline_and_auto_analyze
            from app.crashguard.services.job_heartbeat import record_heartbeat

            async def _bg_pipeline():
                try:
                    async with record_heartbeat("pipeline") as hb:
                        res = await run_pipeline_and_auto_analyze(reason="auto_retry")
                        hb.set_summary({**(res or {}), "via": "auto_retry"})
                except Exception:
                    logger.exception("auto_retry pipeline crashed")

            asyncio.create_task(_bg_pipeline())
            return True, "pipeline: scheduled in background"
        # morning_daily / evening_daily 不主动重发（避免重复推卡片）
        return False, f"no_retry_runner_for:{job_name}"
    except Exception as exc:
        logger.exception("auto-retry %s crashed", job_name)
        return False, f"retry_exception: {exc}"


def _is_weekend_local() -> bool:
    """北京时间是否周末。容器内 TZ=Asia/Shanghai。"""
    return datetime.now().weekday() >= 5  # 5=Sat, 6=Sun


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
        {"name": "top_crash_auto_pr", "cron_field": "top_crash_auto_pr_cron"},
    ]

    now_utc = datetime.utcnow()
    cooldown_min = int(getattr(s, "job_health_alert_cooldown_minutes", 30) or 30)
    # 周末告警节流倍数（默认 4× = 周末 2h 才能发一次同任务告警）
    weekend_mult = int(getattr(s, "job_health_alert_weekend_multiplier", 4) or 4)
    if _is_weekend_local() and weekend_mult > 1:
        cooldown_min = cooldown_min * weekend_mult
        logger.info("job_health_alert weekend mode: cooldown=%dmin (x%d)",
                    cooldown_min, weekend_mult)
    cooldown = timedelta(minutes=cooldown_min)
    # 失败次数阈值（默认 2，原来 3）
    fail_threshold = int(getattr(s, "job_health_alert_fail_threshold", 2) or 2)
    # degraded 弱信号阈值（连续 N 次部分失败才升级为 failing）
    # 默认 6 = pr_sync 30min 间隔下连续 3h 都 degraded 才告警
    degraded_threshold = int(
        getattr(s, "job_health_alert_degraded_threshold", 6) or 6
    )
    # 自愈重试间隔（默认 10min，防短时间内重复重跑）
    retry_throttle = timedelta(minutes=int(
        getattr(s, "job_health_alert_retry_throttle_minutes", 10) or 10
    ))

    unhealthy: List[Dict[str, Any]] = []
    auto_retried: List[str] = []

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
            # 连续非 success（含 degraded + failed）—— degraded 弱信号通道
            # 抓手：单条 degraded 不告警，但持续 N 次说明系统真有问题
            consecutive_unhealthy = 0
            for r in recent:
                if r.status in ("degraded", "failed"):
                    consecutive_unhealthy += 1
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
            elif consecutive_failures >= fail_threshold:
                health = "failing"
            elif consecutive_unhealthy >= degraded_threshold:
                # 持续 degraded（混 failed 也算）—— 系统性 transient 演变为
                # systemic，进入 failing 状态发告警
                health = "failing"
            else:
                health = "ok"

            if health == "ok":
                # 健康恢复：清自愈失败计数 + 清 retry 时间戳
                if jn in _consecutive_retry_failures:
                    logger.info("job %s recovered, clear retry_failures=%d",
                                jn, _consecutive_retry_failures[jn])
                _consecutive_retry_failures.pop(jn, None)
                _last_retried_at.pop(jn, None)
                continue

            # 评估上次自愈是否成功：自从 last_retried_at 之后是否产生新的 success 心跳？
            last_retry = _last_retried_at.get(jn)
            if last_retry is not None:
                new_success = (await session.execute(
                    select(CrashJobHeartbeat)
                    .where(
                        CrashJobHeartbeat.job_name == jn,
                        CrashJobHeartbeat.status == "success",
                        CrashJobHeartbeat.fired_at > last_retry,
                    )
                    .order_by(desc(CrashJobHeartbeat.fired_at))
                    .limit(1)
                )).scalars().first()
                if new_success:
                    # 自愈成功，但当前 health 仍 != ok 说明又坏了——开新一轮
                    _consecutive_retry_failures.pop(jn, None)
                    _last_retried_at.pop(jn, None)
                    last_retry = None
                elif (now_utc - last_retry) >= retry_throttle:
                    # 距上次重试过了节流窗口仍无 success → 计一次失败
                    _consecutive_retry_failures[jn] = _consecutive_retry_failures.get(jn, 0) + 1
                    logger.info("job %s retry deemed failed, count=%d",
                                jn, _consecutive_retry_failures[jn])
                    _last_retried_at.pop(jn, None)
                    last_retry = None
                # else: 节流窗口内，等待结果，本 tick 跳过
                else:
                    continue

            fail_count = _consecutive_retry_failures.get(jn, 0)

            # 计数 < 阈值 → 触发自愈重跑（不告警）
            if fail_count < RETRY_FAILURE_THRESHOLD:
                ok_retry, retry_summary = await _try_auto_retry_job(jn)
                _last_retried_at[jn] = now_utc
                auto_retried.append(f"{jn}:{retry_summary}(fail_count={fail_count})")
                logger.info(
                    "job_health_alert auto-retried %s: ok=%s fail_count=%d summary=%s",
                    jn, ok_retry, fail_count, retry_summary,
                )
                continue

            # 计数 ≥ 阈值 → 升级为告警
            # 节流：距上次告警 < cooldown 跳过
            last_alert_at = _last_alerted_at.get(jn)
            if last_alert_at is not None and (now_utc - last_alert_at) < cooldown:
                continue

            unhealthy.append({
                "job_name": jn,
                "health": health,
                "consecutive_failures": consecutive_failures,
                "retry_failures": fail_count,
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
        return {
            "ok": True, "alerted": False, "scanned": len(job_meta),
            "auto_retried": auto_retried,
        }

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
        # 路由：alert_email > chat_id > target_email；job_health 是非早晚报告警，走点对点
        if s.feishu_alert_email:
            sent_ok = await send_interactive_card(email=s.feishu_alert_email, card=card)
        elif s.feishu_target_chat_id:
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
        "auto_retried": auto_retried,
    }
