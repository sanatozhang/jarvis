"""Tests for app.services.date_window — pure functions, no DB, no LLM.

All "today" values are injected explicitly (no freezegun in this repo), and
the century/decade prefix is picked to sit outside normal calendar
confusion (real weekdays double-checked below).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services import date_window as dw


# ---------------------------------------------------------------------------
# week_start_of
# ---------------------------------------------------------------------------

def test_week_start_of_midweek():
    # 2026-08-12 is a Wednesday -> Monday of that week is 2026-08-10.
    assert dw.week_start_of(date(2026, 8, 12)) == date(2026, 8, 10)


def test_week_start_of_on_monday_returns_same_day():
    assert dw.week_start_of(date(2026, 8, 10)) == date(2026, 8, 10)


def test_week_start_of_on_sunday_off_by_one_regression():
    # 2026-08-16 is a Sunday -> still belongs to the week starting 08-10,
    # not 08-17. This is the classic off-by-one for weekday()-based logic.
    assert dw.week_start_of(date(2026, 8, 16)) == date(2026, 8, 10)


def test_week_start_of_cross_year_forward():
    # 2027-01-01 is a Friday -> Monday of that week is 2026-12-28 (prior year).
    assert dw.week_start_of(date(2027, 1, 1)) == date(2026, 12, 28)


def test_week_start_of_cross_year_backward():
    # 2026-01-01 is a Thursday -> Monday of that week is 2025-12-29 (prior year).
    assert dw.week_start_of(date(2026, 1, 1)) == date(2025, 12, 29)


def test_week_start_of_leap_day():
    # 2028-02-29 is a Tuesday (2028 is a leap year) -> Monday is 2028-02-28.
    assert dw.week_start_of(date(2028, 2, 29)) == date(2028, 2, 28)


# ---------------------------------------------------------------------------
# current_week / last_week
# ---------------------------------------------------------------------------

def test_current_week_right_edge_is_today_not_sunday():
    start, end = dw.current_week(date(2026, 8, 12))
    assert (start, end) == (date(2026, 8, 10), date(2026, 8, 12))


def test_last_week_from_monday():
    start, end = dw.last_week(date(2026, 8, 10))
    assert (start, end) == (date(2026, 8, 3), date(2026, 8, 9))


def test_last_week_cross_year():
    start, end = dw.last_week(date(2026, 1, 1))
    assert (start, end) == (date(2025, 12, 22), date(2025, 12, 28))


# ---------------------------------------------------------------------------
# default_week_start consistency with week_start_of
# ---------------------------------------------------------------------------

def test_default_week_start_matches_week_start_of_minus_7d():
    today = date(2026, 8, 12)
    from datetime import timedelta
    expected = (dw.week_start_of(today) - timedelta(days=7)).isoformat()
    assert dw.default_week_start(today) == expected


def test_default_week_start_mid_week():
    assert dw.default_week_start(date(2026, 8, 12)) == "2026-08-03"


def test_default_week_start_on_monday():
    assert dw.default_week_start(date(2026, 8, 10)) == "2026-08-03"


# ---------------------------------------------------------------------------
# resolve_window
# ---------------------------------------------------------------------------

def test_resolve_window_days_includes_today():
    date_from, date_to = dw.resolve_window(7, None, None, default_days=30, today=date(2026, 8, 12))
    assert (date_from, date_to) == ("2026-08-06", "2026-08-12")


def test_resolve_window_no_params_uses_default_days():
    date_from, date_to = dw.resolve_window(None, None, None, default_days=30, today=date(2026, 8, 12))
    assert date_from == "2026-07-14"
    assert date_to == "2026-08-12"


def test_resolve_window_explicit_range_ignores_days():
    date_from, date_to = dw.resolve_window(999, "2026-08-01", "2026-08-07", default_days=30, today=date(2026, 8, 12))
    assert (date_from, date_to) == ("2026-08-01", "2026-08-07")


def test_resolve_window_only_date_from_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_window(None, "2026-08-01", None, default_days=30)


def test_resolve_window_only_date_to_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_window(None, None, "2026-08-01", default_days=30)


def test_resolve_window_from_after_to_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_window(None, "2026-08-10", "2026-08-03", default_days=30)


def test_resolve_window_invalid_calendar_date_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_window(None, "2026-02-30", "2026-03-01", default_days=30)


def test_resolve_window_malformed_date_string_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_window(None, "20260212", "20260213", default_days=30)


def test_resolve_window_span_too_large_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_window(None, "2016-01-01", "2026-08-12", default_days=30)


def test_resolve_window_respects_custom_max_span():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_window(None, "2026-01-01", "2026-12-31", default_days=30, max_span_days=90)


# ---------------------------------------------------------------------------
# to_datetime_bounds
# ---------------------------------------------------------------------------

def test_to_datetime_bounds_single_day_window_is_nonempty():
    start, end = dw.to_datetime_bounds("2026-08-10", "2026-08-10")
    assert start.isoformat() == "2026-08-10T00:00:00"
    assert end.isoformat() == "2026-08-10T23:59:59"
    assert start < end


# ---------------------------------------------------------------------------
# month_start_of / current_month / last_month / default_month_start
# ---------------------------------------------------------------------------

def test_month_start_of_midmonth():
    assert dw.month_start_of(date(2026, 8, 12)) == date(2026, 8, 1)


def test_current_month_right_edge_is_today():
    start, end = dw.current_month(date(2026, 8, 12))
    assert (start, end) == (date(2026, 8, 1), date(2026, 8, 12))


def test_last_month_same_year():
    start, end = dw.last_month(date(2026, 8, 12))
    assert (start, end) == (date(2026, 7, 1), date(2026, 7, 31))


def test_last_month_cross_year():
    start, end = dw.last_month(date(2026, 1, 1))
    assert (start, end) == (date(2025, 12, 1), date(2025, 12, 31))


def test_last_month_shorter_february():
    # 2026 is not a leap year -> February has 28 days.
    start, end = dw.last_month(date(2026, 3, 1))
    assert (start, end) == (date(2026, 2, 1), date(2026, 2, 28))


def test_default_month_start_matches_last_month():
    today = date(2026, 8, 12)
    start, _ = dw.last_month(today)
    assert dw.default_month_start(today) == start.isoformat()


# ---------------------------------------------------------------------------
# resolve_period
# ---------------------------------------------------------------------------

def test_resolve_period_completed_week():
    b = dw.resolve_period("week", "2026-08-03", today=date(2026, 8, 12))
    assert (b.date_from, b.date_to) == ("2026-08-03", "2026-08-09")
    assert (b.prev_from, b.prev_to) == ("2026-07-27", "2026-08-02")
    assert b.in_progress is False


def test_resolve_period_in_progress_week_mirrors_partial_span():
    b = dw.resolve_period("week", "2026-08-10", today=date(2026, 8, 12))
    assert (b.date_from, b.date_to) == ("2026-08-10", "2026-08-12")
    assert (b.prev_from, b.prev_to) == ("2026-08-03", "2026-08-05")
    assert b.in_progress is True


def test_resolve_period_completed_month_uses_natural_prior_month_end():
    # July (31 days) baselined against June (30 days) — must NOT compute
    # prev_to as prev_start + 30 (which would land on July 1st).
    b = dw.resolve_period("month", "2026-07-01", today=date(2026, 8, 12))
    assert (b.date_from, b.date_to) == ("2026-07-01", "2026-07-31")
    assert (b.prev_from, b.prev_to) == ("2026-06-01", "2026-06-30")
    assert b.in_progress is False


def test_resolve_period_in_progress_month_mirrors_partial_span():
    b = dw.resolve_period("month", "2026-08-01", today=date(2026, 8, 12))
    assert (b.date_from, b.date_to) == ("2026-08-01", "2026-08-12")
    assert (b.prev_from, b.prev_to) == ("2026-07-01", "2026-07-12")
    assert b.in_progress is True


def test_resolve_period_month_cross_year():
    b = dw.resolve_period("month", "2026-01-01", today=date(2026, 8, 12))
    assert (b.date_from, b.date_to) == ("2026-01-01", "2026-01-31")
    assert (b.prev_from, b.prev_to) == ("2025-12-01", "2025-12-31")


def test_resolve_period_week_non_monday_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_period("week", "2026-08-04", today=date(2026, 8, 12))


def test_resolve_period_month_not_first_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_period("month", "2026-08-15", today=date(2026, 8, 12))


def test_resolve_period_unsupported_type_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_period("quarter", "2026-08-01", today=date(2026, 8, 12))


def test_resolve_period_future_start_raises():
    with pytest.raises(dw.InvalidWindow):
        dw.resolve_period("week", "2026-08-17", today=date(2026, 8, 12))


def test_resolve_period_month_starting_on_monday_still_resolves_as_month():
    # 2025-12-01 happens to be a Monday — the (period_type, week_start)
    # composite key is what disambiguates this from the week period sharing
    # the same date string, not resolve_period itself.
    b = dw.resolve_period("month", "2025-12-01", today=date(2026, 8, 12))
    assert (b.date_from, b.date_to) == ("2025-12-01", "2025-12-31")
