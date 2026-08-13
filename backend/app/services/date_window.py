"""Date-window resolution shared by analytics/voc endpoints — pure stdlib, no
app-internal imports, so this stays a leaf in the lint-imports dependency
graph (analytics endpoints shouldn't have to pull in the LLM stack that
voc_digest.py carries just to compute a date range).

Week boundaries always use `date.weekday()` (Monday=0), never
`date.isocalendar()` — ISO week numbers assign the first days of some
Januaries to the prior year's last week, which is not the "calendar week"
semantics this module implements. All "today" anchors are UTC (`today_utc`)
to match `datetime.utcnow()`-based DB timestamps; do not use local time.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

MAX_WINDOW_DAYS = 3650
MAX_MOVERS_SPAN_DAYS = 366

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidWindow(ValueError):
    """Raised for a malformed or out-of-range date window. Callers at the API
    layer should catch this and respond 422, not 500."""


def today_utc() -> date:
    return datetime.utcnow().date()


def week_start_of(d: date) -> date:
    """The Monday of the calendar week containing `d`."""
    return d - timedelta(days=d.weekday())


def current_week(today: Optional[date] = None) -> Tuple[date, date]:
    """(this Monday, today) — the in-progress current week. The right edge
    is `today`, not the coming Sunday, since the week isn't over yet."""
    today = today or today_utc()
    return week_start_of(today), today


def last_week(today: Optional[date] = None) -> Tuple[date, date]:
    """(last Monday, last Sunday) — the most recently completed full week."""
    today = today or today_utc()
    monday = week_start_of(today) - timedelta(days=7)
    return monday, monday + timedelta(days=6)


def default_week_start(today: Optional[date] = None) -> str:
    """Most recent COMPLETE week's Monday (ISO date string). If `today` is
    itself mid-week, that week isn't done yet, so this points at the week
    before it — e.g. on Wed 2026-08-12, returns 2026-08-03 (last Monday),
    not 2026-08-10 (this week's Monday, still in progress). On a Monday,
    "today" is the very start of a new week, so this still returns the
    prior Monday — the week that just finished."""
    monday, _ = last_week(today)
    return monday.isoformat()


def _parse_iso_date(s: str, field: str) -> date:
    # date.fromisoformat() has accepted the basic "YYYYMMDD" form (no
    # dashes) since Python 3.11 — enforce the dashed YYYY-MM-DD shape
    # explicitly so malformed input is rejected as InvalidWindow (422)
    # rather than silently parsed.
    if not _ISO_DATE_RE.match(s):
        raise InvalidWindow(f"{field} is not a valid ISO date: {s!r}")
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise InvalidWindow(f"{field} is not a valid ISO date: {s!r}") from e


def resolve_window(
    days: Optional[int],
    date_from: Optional[str],
    date_to: Optional[str],
    default_days: int,
    today: Optional[date] = None,
    max_span_days: int = MAX_WINDOW_DAYS,
) -> Tuple[str, str]:
    """Resolve the (date_from, date_to) inclusive window from optional
    query params. Priority: explicit date_from/date_to (must be given
    together) > days > default_days. Returns ISO date strings.

    Raises InvalidWindow for: only one of date_from/date_to given,
    unparseable dates, date_from > date_to, or a span exceeding
    max_span_days. Does not clamp date_to into the future — an
    out-of-range-future date_to simply returns no data, which is more
    honest than silently rewriting what the caller asked for.
    """
    if (date_from is None) != (date_to is None):
        raise InvalidWindow("date_from and date_to must be provided together")

    if date_from is not None and date_to is not None:
        d_from = _parse_iso_date(date_from, "date_from")
        d_to = _parse_iso_date(date_to, "date_to")
        if d_from > d_to:
            raise InvalidWindow("date_from must not be after date_to")
        span = (d_to - d_from).days + 1
        if span > max_span_days:
            raise InvalidWindow(f"window spans {span} days, exceeding max of {max_span_days}")
        return d_from.isoformat(), d_to.isoformat()

    today = today or today_utc()
    n = days if days is not None else default_days
    d_from = today - timedelta(days=n - 1)
    return d_from.isoformat(), today.isoformat()


def to_datetime_bounds(date_from: str, date_to: str) -> Tuple[datetime, datetime]:
    """(date_from 00:00:00, date_to 23:59:59) as naive datetimes, for
    BETWEEN-style comparisons against `created_at` columns."""
    start = datetime.fromisoformat(date_from)
    end = datetime.fromisoformat(date_to + "T23:59:59")
    return start, end
