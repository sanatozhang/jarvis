"""Tests for issue text normalization and problem date resolution."""

from datetime import datetime

from app.services.issue_text import guess_problem_date, normalize_description_for_matching


def test_normalize_description_strips_ui_metadata():
    assert normalize_description_for_matching("[APP] [蓝牙连接] 录音找不到") == "录音找不到"


def test_guess_problem_date_prefers_structured_occurred_at():
    occurred_at = datetime(2026, 3, 18, 9, 30, 0)
    assert guess_problem_date("昨天录音找不到", occurred_at=occurred_at) == "2026-03-18"


def test_guess_problem_date_supports_chinese_relative_and_partial_dates():
    now = datetime(2026, 3, 20, 10, 0, 0)
    assert guess_problem_date("昨天录音找不到", now=now) == "2026-03-19"
    assert guess_problem_date("2月3号录音找不到", now=now) == "2026-02-03"
