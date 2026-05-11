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


def test_cron_unsupported_dow_returns_false():
    """带 day-of-week 的不支持 → False（保守）"""
    from app.crashguard.workers.scheduler import _cron_matches
    assert _cron_matches("0 7 * * 1", datetime(2026, 4, 29, 7, 0)) is False


def test_cron_invalid_format_returns_false():
    from app.crashguard.workers.scheduler import _cron_matches
    assert _cron_matches("invalid", datetime.utcnow()) is False
    assert _cron_matches("", datetime.utcnow()) is False
