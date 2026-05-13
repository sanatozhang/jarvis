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
_analyze_last_fired: str = ""    # 定时分析 tick 防同分钟重跑
_hourly_alert_last_fired: str = ""  # 进程级幂等；DB UNIQUE(hour_utc) 兜多机
_core_metric_last_fired: str = ""   # 10min tick 进程级幂等；DB UNIQUE(window_start) 兜多机
_job_health_last_fired: str = ""    # 兜底告警 tick 进程级幂等
_backfill_last_fired: str = ""      # 周度 baseline 回填 tick 进程级幂等


async def _run_analyze_tick(max_per_tick: int) -> dict:
    """分批跑今日 attention 中未 success 的 issue，最多 max_per_tick 个。

    设计目标：单次只跑 1-2 个 issue（~90s/个），避免一次性 20 个被 OS/网络 timeout 杀。
    复用 _auto_analyze_attention 的去重 + 串行 + auto-PR 逻辑。
    """
    from datetime import date
    from app.crashguard.services.daily_report import _auto_analyze_attention
    from app.crashguard.workers.warmup import _collect_attention_ids

    today = date.today()
    full = await _collect_attention_ids(today)
    if not full:
        return {"picked": 0, "completed": 0, "remaining": 0}

    # _auto_analyze_attention 内部还有 dedup 闸再过滤 success/running/pending —— 这里限量挑前 N 个传进去
    # （内部会过滤掉已跑过的）
    picked = full[: max(1, int(max_per_tick))]
    completed = await _auto_analyze_attention(picked)
    return {"picked": len(picked), "completed": completed, "remaining": max(0, len(full) - completed)}


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
    from app.crashguard.services.job_heartbeat import record_heartbeat
    for report_type, cron_expr in schedule:
        if _last_fired.get(report_type) == tag:
            continue
        if not _cron_matches(cron_expr, now):
            continue
        # 先打 tag 再 send——异常时本分钟内不重试（避免日志风暴），下分钟 cron 不再 match
        _last_fired[report_type] = tag
        job_name = "morning_daily" if report_type == "morning" else "evening_daily"
        try:
            async with record_heartbeat(job_name) as hb:
                res = await send_daily_report(report_type, target_date=now.date())
                hb.set_summary(res)
                hb.set_status_from_result(res)
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
        _pr_sync_last_fired = tag
        try:
            async with record_heartbeat("pr_sync") as hb:
                from app.crashguard.services.pr_sync import sync_all_open_prs
                res = await sync_all_open_prs()
                hb.set_summary(res)
                if res.get("errors", 0) > 0:
                    hb.status = "failed"
                    hb.error = f"errors={res.get('errors')}"
                logger.info(
                    "crashguard pr_sync fired: checked=%d changed=%d errors=%d",
                    res.get("checked", 0), res.get("changed", 0), res.get("errors", 0),
                )
        except Exception:
            logger.exception("crashguard pr_sync tick failed")

    # AI 分析定时小步分批（独立 cron，默认 */5）
    global _analyze_last_fired
    analyze_cron = getattr(s, "analyze_cron", "") or ""
    if analyze_cron and _analyze_last_fired != tag and _cron_matches(analyze_cron, now):
        _analyze_last_fired = tag  # 先打 tag 防异常重试
        max_per_tick = int(getattr(s, "analyze_max_per_tick", 1) or 1)
        try:
            async with record_heartbeat("analyze_tick") as hb:
                res = await _run_analyze_tick(max_per_tick=max_per_tick)
                hb.set_summary(res)
                if res.get("picked", 0) == 0:
                    hb.status = "skipped"
                logger.info(
                    "crashguard analyze tick fired: picked=%d completed=%d remaining=%d",
                    res.get("picked", 0), res.get("completed", 0), res.get("remaining", 0),
                )
        except Exception:
            logger.exception("crashguard analyze tick failed")

    # Hourly alert（SHoW 对比；独立 cron，默认每小时第 5 分钟）
    global _hourly_alert_last_fired
    if getattr(s, "hourly_alert_enabled", False):
        hourly_cron = getattr(s, "hourly_alert_cron", "") or ""
        if hourly_cron and _hourly_alert_last_fired != tag and _cron_matches(hourly_cron, now):
            _hourly_alert_last_fired = tag  # 先打 tag 防异常重试
            try:
                async with record_heartbeat("hourly_alert") as hb:
                    from app.crashguard.services.hourly_alerter import run_hourly_alert_tick
                    res = await run_hourly_alert_tick()
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                    logger.info(
                        "crashguard hourly_alert tick fired: alerted=%s new=%s surge=%s reason=%s",
                        res.get("alerted"), res.get("new"), res.get("surge"),
                        res.get("reason") or res.get("skipped") or res.get("error", ""),
                    )
            except Exception:
                logger.exception("crashguard hourly_alert tick failed")

    # 核心指标告警（crash-free sessions %，10 分钟粒度）
    global _core_metric_last_fired
    if getattr(s, "core_metric_enabled", False):
        cm_cron = getattr(s, "core_metric_cron", "") or ""
        if cm_cron and _core_metric_last_fired != tag and _cron_matches(cm_cron, now):
            _core_metric_last_fired = tag
            try:
                async with record_heartbeat("core_metric") as hb:
                    from app.crashguard.services.core_metric_alerter import run_core_metric_tick
                    res = await run_core_metric_tick()
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                logger.info(
                    "crashguard core_metric tick fired: alerted=%s direction=%s reason=%s",
                    res.get("alerted"), res.get("direction"),
                    res.get("reason") or res.get("skipped") or res.get("error", ""),
                )
            except Exception:
                logger.exception("crashguard core_metric tick failed")

    # Baseline 回填（每周一次扫近 3 天，补 hourly_alert tick 漏掉的窗口 + 早晚报 DB fallback）
    # 底层逻辑：日常 pipeline / hourly_alert tick 若因 Datadog 限流或重启失败，会留下基线
    # 窗口空洞。每周扫一遍最近 3 天，幂等 INSERT OR IGNORE 补齐缺口，保证 SHoW 基线始终有数据。
    global _backfill_last_fired
    if getattr(s, "baseline_backfill_enabled", True):
        bf_cron = getattr(s, "baseline_backfill_cron", "") or "0 18 * * 0"
        if bf_cron and _backfill_last_fired != tag and _cron_matches(bf_cron, now):
            _backfill_last_fired = tag
            try:
                async with record_heartbeat("baseline_backfill") as hb:
                    from app.crashguard.scripts_runtime import run_backfill_all
                    res = await run_backfill_all(days_hourly=3, days_daily=3)
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                    logger.info(
                        "crashguard baseline_backfill fired: hourly_written=%s daily_written=%s",
                        res.get("hourly", {}).get("written"),
                        res.get("daily", {}).get("written"),
                    )
            except Exception:
                logger.exception("crashguard baseline_backfill tick failed")

    # 定时任务健康度兜底告警（每 5min 扫描 heartbeat 表）
    global _job_health_last_fired
    if getattr(s, "job_health_alert_enabled", True):
        jh_cron = getattr(s, "job_health_alert_cron", "") or ""
        if jh_cron and _job_health_last_fired != tag and _cron_matches(jh_cron, now):
            _job_health_last_fired = tag
            try:
                async with record_heartbeat("job_health_alert") as hb:
                    from app.crashguard.services.job_health_alerter import run_job_health_check
                    res = await run_job_health_check()
                    hb.set_summary(res)
                    hb.set_status_from_result(res)
                    logger.info(
                        "crashguard job_health_alert fired: alerted=%s unhealthy=%s",
                        res.get("alerted"),
                        res.get("unhealthy_jobs") or res.get("skipped") or res.get("error", ""),
                    )
            except Exception:
                logger.exception("crashguard job_health_alert tick failed")


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
