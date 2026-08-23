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

`degraded`/`failed` 现在都会额外私聊告警 `settings.alert_email`（2026-08-23 加，
见 `_send_failure_alert`）——之前只写心跳表，没人主动查就等于没人知道，8/21~8/22
连续两天报告构建崩溃、飞书群什么都没收到都是这样被"发现"的。告警发送本身失败
不能反过来把这次 job 也标记失败，全程包 try/except，只 log 不上抛。
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
from app.graygate.services.card_builder import build_report_card
from app.services.feishu_cli import send_interactive_card, send_message

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


async def _send_failure_alert(status: str, target_date: date, error: Optional[str], summary: dict) -> None:
    """`degraded`/`failed` 时私聊告警——绝不能因为告警本身发不出去就影响 job 结果。"""
    settings = get_graygate_settings()
    target = getattr(settings, "alert_email", "") or "sanato.zhang@plaud.ai"
    if status == "failed":
        text = (
            f"🔴 4.0 灰度日报构建失败（{target_date.isoformat()}），今天不会发到群里。\n"
            f"error: {error}\n"
            "需要人工看一下日志排查（心跳表 coreguard_job_heartbeats，"
            "job_name=graygate_daily_report）。"
        )
    else:
        text = (
            f"🟡 4.0 灰度日报数据算出来了但发送失败（{target_date.isoformat()}），群里没收到。\n"
            f"summary: {summary}\n"
            "可能是飞书 API 抖动，也可能是 chat_id 配置有问题，需要人工确认。"
        )
    try:
        await send_message(email=target, text=text)
    except Exception:
        logger.exception("graygate_daily_report: failed to send failure alert itself")


async def _run_daily_report_once(target_date: date) -> None:
    """跑一次 4.0.3 灰度日报（含写心跳）。见模块 docstring 的三态说明。"""
    start = time.monotonic()
    status = "success"
    error: Optional[str] = None
    summary: dict = {"target_date": target_date.isoformat()}

    try:
        settings = get_graygate_settings()
        report = await build_report_card(target_date)
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
                sent = await send_interactive_card(chat_id=settings.feishu_chat_id, card=report.card)
            except Exception as e:
                logger.warning("graygate_daily_report: send_interactive_card raised: %s", e)
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
    if status in ("failed", "degraded"):
        await _send_failure_alert(status, target_date, error, summary)


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
