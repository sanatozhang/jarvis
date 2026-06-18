"""scheduler 每日报告 catch-up 触发判定单测。

回归根因：单线程 60s loop 顺序 await，长任务（如 ~9min analyze_tick）会让 loop 跳过整
分钟，早报 cron `0 8` 一天只有 08:00 一次机会，错过即全天不发（2026-06-18 实际丢报）。
catch-up 改成「到点且当天未发就补发」，08:00 被跳过、08:05 仍能补上。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import app.crashguard.workers.scheduler as sched
from app.crashguard.workers.scheduler import (
    _DAILY_CATCHUP_GRACE_SEC,
    _daily_fire_decision,
    _parse_fixed_daily,
)


def test_parse_fixed_daily_ok():
    assert _parse_fixed_daily("0 8 * * *") == (0, 8)
    assert _parse_fixed_daily("30 17 * * *") == (30, 17)


def test_parse_fixed_daily_rejects_non_fixed():
    assert _parse_fixed_daily("*/5 * * * *") is None      # 步进
    assert _parse_fixed_daily("0 8 * * 1") is None         # 限定周一
    assert _parse_fixed_daily("0 */4 * * *") is None       # 每 4 小时
    assert _parse_fixed_daily("0 8 * *") is None           # 字段数不对
    assert _parse_fixed_daily("") is None
    assert _parse_fixed_daily("99 8 * * *") is None        # 分钟越界


def test_non_fixed_cron_falls_back_to_none():
    # 非固定 cron → None，调用方回退精确匹配
    assert _daily_fire_decision("*/5 * * * *", datetime(2026, 6, 18, 8, 0), None) is None


def test_fires_exactly_on_time():
    should, tag = _daily_fire_decision("0 8 * * *", datetime(2026, 6, 18, 8, 0, 0), None)
    assert should is True
    assert tag == "2026-06-18"


def test_catchup_fires_after_skipped_minute():
    # 核心回归：08:00 那一分钟被长任务吞掉，08:05 的下一个 tick 仍应补发
    should, tag = _daily_fire_decision("0 8 * * *", datetime(2026, 6, 18, 8, 5, 13), None)
    assert should is True
    assert tag == "2026-06-18"


def test_does_not_fire_before_scheduled():
    should, _ = _daily_fire_decision("0 8 * * *", datetime(2026, 6, 18, 7, 59, 0), None)
    assert should is False


def test_does_not_fire_beyond_grace():
    # 超过宽限（默认 2h）→ 过期不补，避免重启后深夜补发"早报"
    past_grace = datetime(2026, 6, 18, 8, 0) + _td(_DAILY_CATCHUP_GRACE_SEC + 60)
    should, _ = _daily_fire_decision("0 8 * * *", past_grace, None)
    assert should is False


def test_within_grace_edge_fires():
    within = datetime(2026, 6, 18, 8, 0) + _td(_DAILY_CATCHUP_GRACE_SEC - 60)
    should, _ = _daily_fire_decision("0 8 * * *", within, None)
    assert should is True


def test_does_not_double_fire_same_day():
    # 当天已发过（in-memory 幂等）→ 不再补发
    should, _ = _daily_fire_decision("0 8 * * *", datetime(2026, 6, 18, 8, 5), "2026-06-18")
    assert should is False


def test_fires_again_next_day():
    # 昨天发过，今天到点应再发
    should, tag = _daily_fire_decision("0 8 * * *", datetime(2026, 6, 19, 8, 1), "2026-06-18")
    assert should is True
    assert tag == "2026-06-19"


def _td(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)


# ── heavy-job 串行 worker ──────────────────────────────────────────────

def _reset_worker():
    sched._job_queue = None
    sched._queued_jobs = set()


async def _drain(timeout=2.0):
    """跑 worker 直到队列清空，然后收掉它。"""
    worker = asyncio.create_task(sched._job_worker_loop())
    try:
        await asyncio.wait_for(sched._get_job_queue().join(), timeout=timeout)
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


async def test_enqueue_dedup_same_job_not_queued_twice():
    _reset_worker()
    async def f():
        pass
    sched._enqueue_job("jobA", f)
    sched._enqueue_job("jobA", f)   # 上一次还没跑完 → 不重复入队
    assert sched._get_job_queue().qsize() == 1


async def test_worker_runs_job_and_clears_flag():
    _reset_worker()
    ran = []
    async def f():
        ran.append("done")
    sched._enqueue_job("jobX", f)
    await _drain()
    assert ran == ["done"]
    assert "jobX" not in sched._queued_jobs   # 跑完清标志 → 下次可再入队


async def test_worker_serializes_jobs():
    _reset_worker()
    order = []
    async def slow():
        order.append("slow_start"); await asyncio.sleep(0.05); order.append("slow_end")
    async def fast():
        order.append("fast")
    sched._enqueue_job("slow", slow)
    sched._enqueue_job("fast", fast)
    await _drain()
    # 串行执行：fast 不会插进 slow 中间
    assert order == ["slow_start", "slow_end", "fast"]


async def test_worker_survives_job_exception():
    _reset_worker()
    ran = []
    async def boom():
        raise RuntimeError("boom")
    async def ok():
        ran.append("ok")
    sched._enqueue_job("boom", boom)
    sched._enqueue_job("ok", ok)
    await _drain()
    assert ran == ["ok"]                  # 一个 job 抛异常不影响后续
    assert sched._queued_jobs == set()    # 异常路径也清标志
