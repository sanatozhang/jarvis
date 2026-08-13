"""Shared date-window FastAPI dependency for analytics/voc endpoints.

Consolidates the "days OR explicit date_from/date_to" query-param pattern
that used to be copy-pasted (with a one-day-off inconsistency) across
analytics.py and voc.py.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

from fastapi import HTTPException, Query

from app.services.date_window import InvalidWindow, resolve_window

_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def window_params(
    default_days: int,
    max_span_days: int = 3650,
) -> Callable[..., Tuple[str, str]]:
    """Build a FastAPI dependency resolving to (date_from, date_to) ISO
    strings. `days` defaults to None (not `default_days`) so the dependency
    can tell "not passed" apart from "explicitly passed" — the priority
    rule (explicit date_from/date_to > days > default_days) depends on it.
    """
    def dep(
        days: Optional[int] = Query(
            None, ge=1, le=3650,
            description=f"Lookback days (default {default_days} when omitted); ignored when date_from/date_to are given",
        ),
        date_from: Optional[str] = Query(None, pattern=_DATE_PATTERN, description="Inclusive start date, YYYY-MM-DD"),
        date_to: Optional[str] = Query(None, pattern=_DATE_PATTERN, description="Inclusive end date, YYYY-MM-DD"),
    ) -> Tuple[str, str]:
        try:
            return resolve_window(days, date_from, date_to, default_days, max_span_days=max_span_days)
        except InvalidWindow as e:
            raise HTTPException(status_code=422, detail=str(e))
    return dep
