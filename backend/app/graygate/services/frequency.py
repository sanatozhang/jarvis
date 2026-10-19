"""Graygate 报告发布频率（设置页开关，2026-10-19）。

| 取值 | 含义 |
|---|---|
| `daily`（默认）| 只发每天 9 点那份「昨天全天」日报 |
| `30m` / `2h` / `6h` / `12h` | 日报照发，**另外**按这个间隔发「今日至今」报告 |

「今日至今」= 当天 BJT 00:00 → 发送时刻，对比上一个工作日的同一时段（00:00 →
同一钟点）。发送时刻对齐整点槽位（30m → :00/:30，2h → 偶数整点…），00:00 那个
槽位窗口为空，跳过。

存储：`oncall_config` 表的一行（跟 `notify_switch` 同一张 KV 表，不新建表）。
取值在运行中的 settings 单例上（`report_frequency`），设置页改完立刻生效；
启动时由 scheduler 回灌（`apply_frequency_from_db`）。
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Dict, Optional

from app.db import database as db
from app.graygate.config import get_graygate_settings

logger = logging.getLogger("graygate.frequency")

FREQUENCY_KEY = "graygate_report_frequency"
DEFAULT_FREQUENCY = "daily"
# 取值 → 间隔分钟数；daily 没有高频报告
FREQUENCY_MINUTES: Dict[str, int] = {"daily": 0, "30m": 30, "2h": 120, "6h": 360, "12h": 720}


def current_frequency() -> str:
    f = getattr(get_graygate_settings(), "report_frequency", DEFAULT_FREQUENCY)
    return f if f in FREQUENCY_MINUTES else DEFAULT_FREQUENCY


async def set_frequency(frequency: str) -> str:
    f = (frequency or "").strip().lower()
    if f not in FREQUENCY_MINUTES:
        raise ValueError(f"未知频率 {frequency!r}；可选：{list(FREQUENCY_MINUTES)}")
    await db.set_oncall_config(FREQUENCY_KEY, f)
    get_graygate_settings().report_frequency = f
    logger.info("graygate report frequency → %s", f)
    return f


async def apply_frequency_from_db() -> None:
    """启动期回灌；DB 里没有 / 值非法就保持默认。"""
    try:
        f = await db.get_oncall_config(FREQUENCY_KEY, "")
    except Exception:
        logger.exception("graygate frequency: DB read failed, keep %s", current_frequency())
        return
    if f in FREQUENCY_MINUTES:
        get_graygate_settings().report_frequency = f


def current_slot(now_bjt: datetime, frequency: Optional[str] = None) -> Optional[datetime]:
    """`now_bjt` 所在的最近一个已到达的槽位起点；daily 或 00:00 槽位（窗口为空）返回 None。"""
    minutes = FREQUENCY_MINUTES.get(frequency or current_frequency(), 0)
    if not minutes:
        return None
    since_midnight = now_bjt.hour * 60 + now_bjt.minute
    slot_min = since_midnight - since_midnight % minutes
    if slot_min == 0:
        return None
    return datetime.combine(now_bjt.date(), time(slot_min // 60, slot_min % 60), tzinfo=now_bjt.tzinfo)
