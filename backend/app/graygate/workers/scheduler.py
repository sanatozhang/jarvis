"""Graygate 每日报告调度（60s tick，参照 `app.coreguard.workers.scheduler` 的极简模式）。

与 coreguard 调度器的关键差异（task-6-brief.md 明确要求，照抄前先读那份 brief）：

- 判断"是否到了发送时刻"必须用 `datetime.now(ZoneInfo("Asia/Shanghai"))`，**禁止**
  `datetime.utcnow()`——这是本项目的真实事故教训（早报延后 8 小时，见
  `backend/CLAUDE.md`「Docker 已知问题 #6」），不要重蹈覆辙。
- 幂等粒度是"天"不是"分钟"：graygate 每天只发一次报告，不是 coreguard
  `coreguard_hourly_watch` 那种"每小时都跑、同一分钟内不重复触发"的 job。进程级
  `_last_fired_date` 记录"今天是否已经发过"，跨天自动重置（比较的是新的一天）。
- `target_date` = 触发时刻的 BJT 昨天：9 点触发时，报的是昨天全天的数据——
  触发当天 0-9 点的数据还不完整，不该算进去。

心跳表复用 `app.coreguard.models.CoreguardJobHeartbeat`（`coreguard_job_heartbeats`
表），`job_name="graygate_daily_report"`。这是本任务经过考虑的决策：graygate 是
临时的灰度期模块，新建一张表要过 migration + 数据库变更流程，成本和收益不成
比例；复用 coreguard 的心跳表纯粹是"借一张通用日志表记一行"，不涉及外键、不跨
`crash_*` 表。`.importlinter` 的 `crashguard-isolation` 合约只限制
`app.crashguard.*` 的 import 方向，不限制 graygate，所以这里 import
`app.coreguard.models` 不违反任何已注册的隔离合约（见 task-6-brief.md）。

三层 kill switch（`GraygateSettings`，见 `app/graygate/config.py`）：
- `enabled=False` 或 `scheduler_enabled=False` → 整个 tick 直接返回，不跑
  `build_report`（省 Datadog 请求）。
- `feishu_enabled=False` → 正常跑 `build_report`（算出数据、写心跳），但跳过
  实际发送——这个开关的语义是"算但不吵群"，不是失败。

三态（写进心跳 `status` 字段）：
- `success`——正常发送，或 `available=False` 时按预期跳过发送，都算 success。
- `degraded`——`build_report` 取数成功，但飞书发送失败（`send_message` 返回 False
  或抛异常）。
- `failed`——`build_report` 本身抛异常。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.db.database import get_session
from app.graygate.config import get_graygate_settings
from app.graygate.services.report_builder import build_report
from app.services.feishu_cli import send_message

logger = logging.getLogger("graygate.scheduler")

_TICK_INTERVAL_SEC = 60
_BJT = ZoneInfo("Asia/Shanghai")
_JOB_NAME = "graygate_daily_report"

_last_fired_date: Optional[date] = None  # 进程级幂等，粒度是"天"（不是"分钟"）


def _now_bjt() -> datetime:
    """就是 `datetime.now(ZoneInfo("Asia/Shanghai"))`——拆成一个小函数纯粹是为了
    让测试能 `monkeypatch.setattr(scheduler, "_now_bjt", ...)` 精确控制"现在几点"，
    不需要去 monkeypatch 标准库 `datetime` 类本身。禁止在这里改回
    `datetime.utcnow()`（见模块 docstring 的事故教训）。
    """
    return datetime.now(_BJT)


async def _write_heartbeat(status: str, duration_ms: int, summary: dict, error: Optional[str] = None) -> None:
    """写 `coreguard_job_heartbeats` 表——字段/写法照抄
    `app.coreguard.workers.scheduler._write_heartbeat`（同一个表模型）。
    """
    try:
        from app.coreguard.models import CoreguardJobHeartbeat
        async with get_session() as session:
            session.add(CoreguardJobHeartbeat(
                job_name=_JOB_NAME,
                fired_at=datetime.utcnow(),
                status=status,
                duration_ms=duration_ms,
                summary=json.dumps(summary, ensure_ascii=False)[:2000],
                error=(error or "")[:1000],
            ))
            await session.commit()
    except Exception as e:
        logger.warning("graygate heartbeat write failed: %s", e)


async def _run_daily_report_once(target_date: date) -> None:
    """跑一次 4.0.3 灰度日报（含写心跳）。见模块 docstring 的三态说明。"""
    start = time.monotonic()
    status = "success"
    error: Optional[str] = None
    summary: dict = {"target_date": target_date.isoformat()}

    try:
        settings = get_graygate_settings()
        report = await build_report(target_date)
        summary["available"] = report.available

        if not report.available:
            # available=False 不是错误——只是两平台版本枚举都是空，没有数据可报。
            # 只写心跳记录这个事实（方便运维知道"今天为什么没发"），不发送、不算失败。
            summary["sent"] = False
        elif not settings.feishu_enabled:
            # feishu_enabled 是"算但不吵群"的总闸，主动跳过发送不是失败。
            summary["sent"] = False
            summary["skip_reason"] = "feishu_enabled=False"
        else:
            sent = False
            try:
                sent = await send_message(chat_id=settings.feishu_chat_id, text=report.markdown)
            except Exception as e:
                logger.warning("graygate_daily_report: send_message raised: %s", e)
            summary["sent"] = sent
            if not sent:
                status = "degraded"
    except Exception as e:
        status = "failed"
        error = repr(e)[:500]
        logger.exception("graygate_daily_report failed")

    duration_ms = int((time.monotonic() - start) * 1000)
    await _write_heartbeat(status, duration_ms, summary, error)
    logger.info(
        "graygate_daily_report done: status=%s duration_ms=%d summary=%s",
        status, duration_ms, summary,
    )


async def _tick_once() -> None:
    """一次 tick：三层 kill switch → 是否命中发送小时 → 进程级幂等（按天）→ 触发。

    直接 `await _run_daily_report_once(...)`（不像 coreguard 那样
    `asyncio.create_task` 丢到后台）：graygate 一天只跑一次，跑一次几秒钟的
    Datadog 查询远不到下一次 60s tick，阻塞式等待既更简单也更容易测试，不需要
    额外处理"后台任务还没跑完就到下一个 tick"的并发场景。
    """
    global _last_fired_date

    settings = get_graygate_settings()
    if not settings.enabled or not settings.scheduler_enabled:
        return

    now_bjt = _now_bjt()
    if now_bjt.hour != settings.report_hour_bjt:
        return
    if _last_fired_date == now_bjt.date():
        return  # 今天已经发过（幂等粒度是"天"）

    _last_fired_date = now_bjt.date()
    target_date = now_bjt.date() - timedelta(days=1)
    logger.info(
        "graygate_daily_report fired at %s (target_date=%s)",
        now_bjt.isoformat(), target_date,
    )
    await _run_daily_report_once(target_date)


async def scheduler_loop() -> None:
    """周期 60s tick。供 `app/main.py` lifespan 里 `asyncio.create_task()` 调用。"""
    logger.info("graygate scheduler starting (interval=%ds)", _TICK_INTERVAL_SEC)
    # 启动后先等一个 tick，避免与启动阶段的其他任务抢跑。
    await asyncio.sleep(5)
    while True:
        try:
            await _tick_once()
        except Exception as e:
            logger.exception("graygate scheduler tick error: %s", e)
        await asyncio.sleep(_TICK_INTERVAL_SEC)
