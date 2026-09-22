"""符号包预热的 cron 分发验证（2026-09-22）。

只验证"代码读起来对"是不够的 —— 这里真的跑 _tick_once()，断言：

1. cron 匹配时 symbol_prewarm 被 **enqueue 到 job queue**（不是直接 await）。
   这条最重要：_tick_once 是 60s 单线程顺序 await，预热要下 90MB，
   直接 await 会拖垮整个 loop（见 scheduler.py line 161 注释）。
2. enabled=False 时不触发。
3. 同一分钟内重复 tick 只 enqueue 一次（进程级幂等）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

import app.crashguard.workers.scheduler as sched


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """每个用例重置 scheduler 的模块级幂等状态 + job queue。"""
    sched._symbol_prewarm_last_fired = ""
    sched._queued_jobs.clear()
    yield
    sched._symbol_prewarm_last_fired = ""
    sched._queued_jobs.clear()


def _stub_settings(monkeypatch, **overrides):
    """真实 CrashguardSettings 为底，只关掉其余 cron —— 隔离出 symbol_prewarm。

    用真实 settings 而非手写 stub：手写的会因为 _tick_once 里读到新字段
    （feishu_target_chat_id 等）而 AttributeError，且随 config 演进不断腐坏。
    """
    from app.crashguard.config import CrashguardSettings

    s = CrashguardSettings()
    # 通过前置闸门
    s.enabled = True
    s.feishu_enabled = True
    s.scheduler_enabled = True
    s.feishu_target_chat_id = "oc_test_chat"
    # 其余任务一律关掉/置空 cron，避免干扰断言
    s.morning_cron = ""
    s.evening_cron = ""
    s.morning_enabled = False
    s.evening_enabled = False
    s.pr_sync_cron = ""
    s.top_crash_auto_pr_cron = ""
    s.analyze_cron = ""
    s.jank_backfill_cron = ""
    s.pipeline_cron = ""
    s.pr_reviewer_daily_cron = ""
    s.pr_pending_review_cron = ""
    s.conflict_resync_cron = ""
    s.hourly_alert_enabled = False
    s.core_metric_enabled = False
    s.baseline_backfill_enabled = False
    s.deep_analysis_auto_enabled = False
    s.job_health_alert_enabled = False
    s.symbol_health_enabled = False
    s.pr_reviewer_enabled = False
    s.pr_pending_review_enabled = False
    s.conflict_resync_enabled = False
    # 本次关注的任务
    s.symbol_prewarm_enabled = True
    s.symbol_prewarm_cron = "*/30 * * * *"

    for k, v in overrides.items():
        setattr(s, k, v)
    monkeypatch.setattr(
        "app.crashguard.config.get_crashguard_settings", lambda: s
    )
    return s


def _capture_enqueued(monkeypatch) -> list:
    """拦 _enqueue_job，记录被 enqueue 的 job 名（不真的执行）。"""
    seen: list = []

    def _fake_enqueue(job_name, coro_factory):
        seen.append(job_name)

    monkeypatch.setattr(sched, "_enqueue_job", _fake_enqueue)
    return seen


@pytest.mark.asyncio
async def test_prewarm_is_enqueued_not_awaited(monkeypatch):
    """核心：预热必须走 job queue。直接 await 会拖垮 60s 主 tick。"""
    _stub_settings(monkeypatch)
    seen = _capture_enqueued(monkeypatch)
    # 12:30 匹配 */30
    monkeypatch.setattr(sched, "datetime", _FrozenDatetime(2026, 9, 22, 12, 30))

    await sched._tick_once()
    assert "symbol_prewarm" in seen


@pytest.mark.asyncio
async def test_prewarm_not_fired_when_disabled(monkeypatch):
    _stub_settings(monkeypatch, symbol_prewarm_enabled=False)
    seen = _capture_enqueued(monkeypatch)
    monkeypatch.setattr(sched, "datetime", _FrozenDatetime(2026, 9, 22, 12, 30))

    await sched._tick_once()
    assert "symbol_prewarm" not in seen


@pytest.mark.asyncio
async def test_prewarm_not_fired_off_cron(monkeypatch):
    """12:17 不匹配 */30。"""
    _stub_settings(monkeypatch)
    seen = _capture_enqueued(monkeypatch)
    monkeypatch.setattr(sched, "datetime", _FrozenDatetime(2026, 9, 22, 12, 17))

    await sched._tick_once()
    assert "symbol_prewarm" not in seen


@pytest.mark.asyncio
async def test_prewarm_idempotent_within_same_minute(monkeypatch):
    """同一分钟内 tick 两次只能 enqueue 一次（_symbol_prewarm_last_fired 幂等）。"""
    _stub_settings(monkeypatch)
    seen = _capture_enqueued(monkeypatch)
    monkeypatch.setattr(sched, "datetime", _FrozenDatetime(2026, 9, 22, 12, 30))

    await sched._tick_once()
    await sched._tick_once()
    assert seen.count("symbol_prewarm") == 1


@pytest.mark.asyncio
async def test_prewarm_empty_cron_never_fires(monkeypatch):
    _stub_settings(monkeypatch, symbol_prewarm_cron="")
    seen = _capture_enqueued(monkeypatch)
    monkeypatch.setattr(sched, "datetime", _FrozenDatetime(2026, 9, 22, 12, 30))

    await sched._tick_once()
    assert "symbol_prewarm" not in seen


class _FrozenDatetime:
    """替 scheduler 模块里的 datetime，让 datetime.now() 返回固定时刻。"""

    def __init__(self, *args):
        self._fixed = datetime(*args)

    def now(self, tz=None):
        return self._fixed

    def __getattr__(self, name):
        return getattr(datetime, name)
