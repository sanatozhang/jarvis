"""
Helpers for extracting the actionable portion of an issue description.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Optional

_LEADING_TAG_RE = re.compile(
    r"^\s*(?:(?:\[[^\]]+\])|(?:【[^】]+】)|(?:\([^)]*\))|(?:（[^）]*）))\s*"
)


def strip_leading_metadata(description: str) -> str:
    """Remove leading UI/category tags like `[APP] [蓝牙连接]` from a description."""
    text = (description or "").strip()
    while text:
        updated = _LEADING_TAG_RE.sub("", text, count=1).strip()
        if updated == text:
            break
        text = updated
    return text


def normalize_description_for_matching(description: str) -> str:
    """
    Return the user-authored issue text with UI/category prefixes removed.

    We intentionally keep the original wording order because phrase order
    matters for downstream matching.
    """
    text = strip_leading_metadata(description)
    text = re.sub(r"\s+", " ", text).strip()
    return text or (description or "").strip()


def guess_problem_date(
    description: str,
    occurred_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Resolve the best available problem date for log filtering."""
    if occurred_at:
        return occurred_at.strftime("%Y-%m-%d")

    text = normalize_description_for_matching(description)
    now = now or datetime.now()

    absolute_patterns = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{4}/\d{2}/\d{2})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
    ]
    for pattern in absolute_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).replace("/", "-")

    chinese_full = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]?", text)
    if chinese_full:
        year, month, day = map(int, chinese_full.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"

    chinese_partial = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
    if chinese_partial:
        month, day = map(int, chinese_partial.groups())
        candidate = datetime(now.year, month, day)
        if candidate.date() > now.date() + timedelta(days=30):
            candidate = datetime(now.year - 1, month, day)
        return candidate.strftime("%Y-%m-%d")

    relative_tokens = {
        "今天": 0,
        "今日": 0,
        "昨天": 1,
        "昨日": 1,
        "前天": 2,
        "today": 0,
        "yesterday": 1,
    }
    lowered = text.lower()
    for token, days_ago in relative_tokens.items():
        haystack = lowered if token.isascii() else text
        if token in haystack:
            return (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    return None
