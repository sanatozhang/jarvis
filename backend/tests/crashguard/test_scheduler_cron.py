"""crashguard 早晚报 cron 解析测试"""
from __future__ import annotations

from datetime import datetime


def test_cron_matches_exact_time():
    from app.crashguard.workers.scheduler import _cron_matches
    assert _cron_matches("0 7 * * *", datetime(2026, 4, 29, 7, 0)) is True
    assert _cron_matches("0 7 * * *", datetime(2026, 4, 29, 7, 1)) is False
    assert _cron_matches("0 7 * * *", datetime(2026, 4, 29, 8, 0)) is False


def test_cron_matches_step_minute():
    from app.crashguard.workers.scheduler import _cron_matches
    assert _cron_matches("*/5 * * * *", datetime(2026, 4, 29, 12, 5)) is True
    assert _cron_matches("*/5 * * * *", datetime(2026, 4, 29, 12, 3)) is False


def test_cron_dow_single_value():
    """day-of-week 单值匹配（Unix 标准 Sun=0...Sat=6）。

    2026-04-29 是周三 → python weekday=2 → cron dow = (2+1)%7 = 3。
    """
    from app.crashguard.workers.scheduler import _cron_matches
    wed = datetime(2026, 4, 29, 7, 0)  # 周三
    assert _cron_matches("0 7 * * 3", wed) is True
    assert _cron_matches("0 7 * * 4", wed) is False  # 周四


def test_cron_dow_range_workdays():
    """1-5 = Mon-Fri 工作日（治本 #1190 pr_pending_review 永不触发的 bug）。"""
    from app.crashguard.workers.scheduler import _cron_matches
    mon = datetime(2026, 4, 27, 10, 0)  # 周一
    tue = datetime(2026, 4, 28, 10, 0)  # 周二
    fri = datetime(2026, 5, 1, 10, 0)   # 周五
    sat = datetime(2026, 5, 2, 10, 0)   # 周六
    sun = datetime(2026, 5, 3, 10, 0)   # 周日
    assert _cron_matches("0 10 * * 1-5", mon) is True
    assert _cron_matches("0 10 * * 1-5", tue) is True
    assert _cron_matches("0 10 * * 1-5", fri) is True
    assert _cron_matches("0 10 * * 1-5", sat) is False
    assert _cron_matches("0 10 * * 1-5", sun) is False


def test_cron_dow_list_format():
    """1,3,5 离散列表（mon/wed/fri）。"""
    from app.crashguard.workers.scheduler import _cron_matches
    mon = datetime(2026, 4, 27, 9, 0)
    wed = datetime(2026, 4, 29, 9, 0)
    fri = datetime(2026, 5, 1, 9, 0)
    tue = datetime(2026, 4, 28, 9, 0)
    assert _cron_matches("0 9 * * 1,3,5", mon) is True
    assert _cron_matches("0 9 * * 1,3,5", wed) is True
    assert _cron_matches("0 9 * * 1,3,5", fri) is True
    assert _cron_matches("0 9 * * 1,3,5", tue) is False


def test_cron_minute_range():
    """minute range 形式 5-10 (5,6,7,8,9,10)。"""
    from app.crashguard.workers.scheduler import _cron_matches
    base = datetime(2026, 4, 29, 9, 0)
    assert _cron_matches("5-10 * * * *", base.replace(minute=5)) is True
    assert _cron_matches("5-10 * * * *", base.replace(minute=8)) is True
    assert _cron_matches("5-10 * * * *", base.replace(minute=10)) is True
    assert _cron_matches("5-10 * * * *", base.replace(minute=11)) is False
    assert _cron_matches("5-10 * * * *", base.replace(minute=4)) is False


def test_cron_invalid_format_returns_false():
    from app.crashguard.workers.scheduler import _cron_matches
    assert _cron_matches("invalid", datetime.utcnow()) is False
    assert _cron_matches("", datetime.utcnow()) is False
