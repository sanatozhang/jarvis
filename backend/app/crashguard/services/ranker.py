"""
Top20 排序器：
- compute_impact_score: crash-free 影响分（用户优先 + 事件加权）
- pick_top_n           : 取 Top N，P0（new/regression）强制入选
"""
from __future__ import annotations

import math
from typing import List


def compute_impact_score(users_affected: int, events_count: int) -> float:
    """
    Crash-free 影响分:
        score = users_affected * log10(events_count + 1)

    底层逻辑: 受影响用户数为主权重，事件次数对数加权（避免单用户死循环刷榜）。
    """
    users = max(0, int(users_affected or 0))
    events = max(0, int(events_count or 0))
    if users == 0 and events == 0:
        return 0.0
    return users * math.log10(events + 1)


from datetime import date
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def pick_top_n(
    session: AsyncSession,
    today: date,
    n: int = 20,
) -> List[Dict[str, Any]]:
    """
    返回 Top N issue（dict 形式）。

    优先级:
    - P0: is_new_in_version OR is_regression → 强制入选
    - P1: 剩余席位按 crash_free_impact_score DESC 填满

    返回字段: datadog_issue_id, title, platform, events_count, users_affected,
             crash_free_impact_score, is_new_in_version, is_regression, is_surge,
             tier ('P0' / 'P1')
    """
    from app.crashguard.models import CrashSnapshot, CrashIssue

    rows = (await session.execute(
        select(CrashSnapshot, CrashIssue)
        .join(
            CrashIssue,
            CrashIssue.datadog_issue_id == CrashSnapshot.datadog_issue_id,
        )
        .where(CrashSnapshot.snapshot_date == today)
    )).all()

    enriched: List[Dict[str, Any]] = []
    for snap, issue in rows:
        enriched.append({
            "datadog_issue_id": snap.datadog_issue_id,
            "title": issue.title or "",
            "platform": issue.platform or "",
            "events_count": snap.events_count or 0,
            "users_affected": snap.users_affected or 0,
            "crash_free_impact_score": snap.crash_free_impact_score or 0.0,
            "is_new_in_version": bool(snap.is_new_in_version),
            "is_regression": bool(snap.is_regression),
            "is_surge": bool(snap.is_surge),
        })

    p0 = [e for e in enriched if e["is_new_in_version"] or e["is_regression"]]
    p1 = [e for e in enriched if not (e["is_new_in_version"] or e["is_regression"])]

    p0.sort(key=lambda e: e["crash_free_impact_score"], reverse=True)
    p1.sort(key=lambda e: e["crash_free_impact_score"], reverse=True)

    selected: List[Dict[str, Any]] = []
    for e in p0[:n]:
        selected.append({**e, "tier": "P0"})
    remaining = n - len(selected)
    if remaining > 0:
        for e in p1[:remaining]:
            selected.append({**e, "tier": "P1"})
    return selected
