"""跨告警类型去重：避免同一 issue 在 hourly_alert / daily_report 之间反复点名。

底层逻辑：
- 用户每天最多接受 ~3-5 条 crashguard 告警；现状 morning + evening + 8 个 hourly
  tick 可能把同一 issue 点 N 次（早晚报 attention pool 不知道 hourly 已点过、
  hourly 也不知道早晚报已包含）。
- 抓手：以 `crash_hourly_alerts.alert_payload` 内的 issue_id 集合作为"近期已告警"
  的事实来源，hourly_alerter 自身做时序去重（12h 内同 issue 不重复点），
  daily_report 在出报时也从 attention 列表里剔除这部分（避免与最新一波 hourly
  内容重复）。
- 颗粒度：12h dedup window 覆盖跨晚报+次日早报，window 可配置。
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timedelta
from typing import Set

from sqlalchemy import select

from app.crashguard.models import CrashHourlyAlert

logger = logging.getLogger("crashguard.alert_dedup")


async def recently_alerted_issue_ids(
    session, since: datetime,
) -> Set[str]:
    """提取 since 之后所有 CrashHourlyAlert.alert_payload 中提到过的 issue_id。

    解析容错：payload 可能是 {"new":[...], "surge":[...]} 也可能是旧版
    {"new_items":[...], "surge_items":[...]}。两种 key 都吃。
    """
    rows = (await session.execute(
        select(CrashHourlyAlert.alert_payload).where(
            CrashHourlyAlert.created_at >= since,
        )
    )).all()
    seen: Set[str] = set()
    for (raw,) in rows:
        if not raw:
            continue
        try:
            p = _json.loads(raw)
        except Exception:
            continue
        # 兼容两套 schema：new/surge vs new_items/surge_items
        for key in ("new", "surge", "new_items", "surge_items"):
            for it in (p.get(key) or []):
                iid = it.get("issue_id") if isinstance(it, dict) else None
                if iid:
                    seen.add(iid)
    return seen


async def recently_alerted_issue_ids_within_hours(
    session, hours: int = 12, now: datetime | None = None,
) -> Set[str]:
    """便捷封装：取最近 N 小时已告警过的 issue_id 集合。
    now 默认 utcnow()；测试可传 fake_now 对齐时序。
    """
    reference = now if now is not None else datetime.utcnow()
    cutoff = reference - timedelta(hours=int(hours or 12))
    return await recently_alerted_issue_ids(session, cutoff)
