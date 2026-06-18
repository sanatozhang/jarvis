"""scheduler 每日报告 catch-up 触发判定单测。

回归根因：单线程 60s loop 顺序 await，长任务（如 ~9min analyze_tick）会让 loop 跳过整
分钟，早报 cron `0 8` 一天只有 08:00 一次机会，错过即全天不发（2026-06-18 实际丢报）。
catch-up 改成「到点且当天未发就补发」，08:00 被跳过、08:05 仍能补上。
"""
from __future__ import annotations

from datetime import datetime

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
