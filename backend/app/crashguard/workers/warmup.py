"""
Crashguard 启动预热 + 周期 pipeline 触发。

用途（闭环抓手）：
- 之前的设计把"自动 AI 分析"嵌套在 send_daily_report 里，必须等 07:00/17:00 cron 才会触发；
  服务重启后到下次 cron 之间是空跑窗口。
- 这里独立出一条"拉数 → 选 Top → 串行 auto-analyze"流水线，启动后异步跑一次（warmup），
  并按 pipeline_cron 周期复跑（默认每 4 小时），与早晚报解耦。

设计取舍：
- 启动延后 60s 再跑，避开应用刚起的初始化抖动；
- fire-and-forget，不阻塞 lifespan startup；
- _auto_analyze_attention 自带去重（success/running 跳过），与早晚报重复触发不会重跑。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import List

from sqlalchemy import select

logger = logging.getLogger("crashguard.warmup")

# 启动 5s 后跑 warmup（之前 60s 太长，重启 → 用户打开 → 空白窗口 1 分钟）
_WARMUP_DELAY_SEC = 5
_PIPELINE_TICK_SEC = 60
_pipeline_last_fired: str = ""


async def _collect_attention_ids(today: date) -> List[str]:
    """从今日 snapshot 选：Top10 fatal + Top10 non_fatal + 全部新增（含 non_fatal）。"""
    from app.crashguard.models import CrashSnapshot
    from app.crashguard.services.ranker import pick_top_n
    from app.db.database import get_session

    candidates: set = set()
    async with get_session() as session:
        top_fatal = await pick_top_n(
            session, today=today, n=10, kinds=(), fatality="fatal", dedup_days=0,
        )
        top_nonfatal = await pick_top_n(
            session, today=today, n=10, kinds=(), fatality="non_fatal", dedup_days=0,
        )
        for item in top_fatal:
            iid = item.get("datadog_issue_id")
            if iid:
                candidates.add(iid)
        for item in top_nonfatal:
            iid = item.get("datadog_issue_id")
            if iid:
                candidates.add(iid)

        new_rows = (await session.execute(
            select(CrashSnapshot.datadog_issue_id).where(
                CrashSnapshot.snapshot_date == today,
                CrashSnapshot.is_new_in_version == True,  # noqa: E712
            )
        )).scalars().all()
        for iid in new_rows:
            if iid:
                candidates.add(iid)

    return sorted(candidates)


async def run_pipeline_and_auto_analyze(reason: str = "warmup") -> dict:
    """跑一次完整闭环：拉数 → 选 Top → 串行 auto-analyze（含 auto-PR）。

    返回 {"issues_processed": int, "attention_count": int, "analyzed": int}
    """
    from app.crashguard.config import get_crashguard_settings
    from app.crashguard.services.daily_report import _auto_analyze_attention
    from app.crashguard.workers.pipeline import run_data_phase

    s = get_crashguard_settings()
    if not s.enabled:
        logger.info("[%s] crashguard disabled, skip", reason)
        return {"issues_processed": 0, "attention_count": 0, "analyzed": 0}
    if not s.datadog_api_key:
        logger.info("[%s] datadog_api_key empty, skip", reason)
        return {"issues_processed": 0, "attention_count": 0, "analyzed": 0}

    today = date.today()
    logger.info("[%s] starting pipeline for %s", reason, today)

    pipeline_result = await run_data_phase(
        today=today, latest_release="", recent_versions=[],
    )
    issues_processed = pipeline_result.get("issues_processed", 0)
    logger.info(
        "[%s] pipeline done: issues=%d top_n=%d",
        reason, issues_processed, pipeline_result.get("top_n_count", 0),
    )

    attention_ids = await _collect_attention_ids(today)
    logger.info("[%s] attention candidates: %d", reason, len(attention_ids))

    analyzed = 0
    if attention_ids:
        analyzed = await _auto_analyze_attention(attention_ids)

    logger.info(
        "[%s] cycle complete: issues=%d attention=%d analyzed=%d",
        reason, issues_processed, len(attention_ids), analyzed,
    )
    return {
        "issues_processed": issues_processed,
        "attention_count": len(attention_ids),
        "analyzed": analyzed,
    }


async def warmup_on_startup() -> None:
    """启动后延后 N 秒跑一次 pipeline + auto-analyze。fire-and-forget。"""
    try:
        await asyncio.sleep(_WARMUP_DELAY_SEC)
    except asyncio.CancelledError:
        return
    try:
        await run_pipeline_and_auto_analyze(reason="warmup")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("warmup pipeline failed (non-fatal)")


def _cron_matches(expr: str, now: datetime) -> bool:
    """复用 scheduler.py 的极简 cron 解析（M H * * * 或 */N 形式）。"""
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


async def pipeline_scheduler_loop() -> None:
    """周期 pipeline 触发（独立于早晚报）。每 60 秒 tick。"""
    from app.crashguard.config import get_crashguard_settings

    logger.info("crashguard pipeline_scheduler_loop started")
    global _pipeline_last_fired
    while True:
        try:
            s = get_crashguard_settings()
            if s.enabled and getattr(s, "scheduler_enabled", True):
                cron = getattr(s, "pipeline_cron", "") or ""
                if cron:
                    now = datetime.now()
                    tag = now.strftime("%Y-%m-%d %H:%M")
                    if _pipeline_last_fired != tag and _cron_matches(cron, now):
                        _pipeline_last_fired = tag
                        try:
                            await run_pipeline_and_auto_analyze(reason="cron")
                        except Exception:
                            logger.exception("pipeline cron tick failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pipeline scheduler tick error (continuing)")
        try:
            await asyncio.sleep(_PIPELINE_TICK_SEC)
        except asyncio.CancelledError:
            raise
