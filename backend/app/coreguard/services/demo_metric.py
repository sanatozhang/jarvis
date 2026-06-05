"""Demo 阶段硬编码的 Crash-free sessions 指标定义。

来源：Datadog dashboard `4h8-qff-zra` widget #0
正式版会由 dashboard_loader 自动从 Datadog API 拉取，这里 demo 阶段先硬编码避免依赖网络。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List


METRIC_KEY = "crash_free_sessions"
METRIC_TITLE = "Crash-free sessions"
VALUE_TYPE = "percent_pp"      # 百分点
DIRECTION = "down_is_bad"
THRESHOLD_PP = 0.5             # 默认阈值；demo_runner 会从 settings 读

# 直接从 dashboard JSON 复制（widget #0 → requests[0]）
CRASH_FREE_SESSIONS_QUERIES: List[Dict[str, Any]] = [
    {
        "name": "query1",
        "data_source": "rum",
        "search": {
            "query": "@type:error @error.is_crash:true -@error.category:ANR env:production @application.name:plaud-flutter @device.type:Mobile",
        },
        "indexes": ["*"],
        "group_by": [],
        "compute": {"aggregation": "cardinality", "metric": "@session.id"},
        "storage": "hot",
    },
    {
        "name": "query2",
        "data_source": "rum",
        "search": {
            "query": "@session.type:user env:production @application.name:plaud-flutter @device.type:Mobile @type:session",
        },
        "indexes": ["*"],
        "group_by": [],
        "compute": {"aggregation": "count"},
    },
]

CRASH_FREE_SESSIONS_FORMULA = "100 - ((query1 * 100) / query2)"

# 用于 sessions_count 守门 (min_baseline_sessions)
SESSIONS_ONLY_QUERIES: List[Dict[str, Any]] = [
    {
        "name": "query2",
        "data_source": "rum",
        "search": {
            "query": "@session.type:user env:production @application.name:plaud-flutter @device.type:Mobile @type:session",
        },
        "indexes": ["*"],
        "group_by": [],
        "compute": {"aggregation": "count"},
    },
]
SESSIONS_ONLY_FORMULA = "query2"


def floor_to_hour(dt: datetime) -> datetime:
    """对齐到 UTC 整点。"""
    return dt.replace(minute=0, second=0, microsecond=0)


def current_window(now: datetime) -> tuple[datetime, datetime]:
    """上一个完整自然小时 [now_hour - 1h, now_hour)。"""
    end = floor_to_hour(now)
    start = end - timedelta(hours=1)
    return start, end


def show_baseline_window(current_start: datetime) -> tuple[datetime, datetime]:
    """[旧机制] 上周同 weekday 同小时。新带引擎用 rolling_window + same_slot_history_windows。"""
    start = current_start - timedelta(days=7)
    end = start + timedelta(hours=1)
    return start, end


def rolling_window(now: datetime, hours: int = 3) -> tuple[datetime, datetime]:
    """滚动评估窗口 [now_floored_to_hour - Nh, now_floored_to_hour)。

    比单自然小时样本量大 N 倍 → 当前值更稳，带更紧。预测带设计 §2.1。
    """
    end = floor_to_hour(now)
    start = end - timedelta(hours=max(1, int(hours)))
    return start, end


def same_slot_history_windows(
    cur_start: datetime, cur_end: datetime, days_back: int
) -> list[tuple[datetime, datetime]]:
    """方案 B：同 2h-of-day × 近 N 天 → 返回 [(start, end)] 各历史同时段窗口。

    offset = 1d, 2d, ..., days_back 天前的同一时段（与当前窗口同样的 time-of-day）。
    预测带设计 §2.2；RUM 仅保留 ~30 天，days_back 应 ≤ 可用天数。
    """
    out: list[tuple[datetime, datetime]] = []
    for d in range(1, max(1, int(days_back)) + 1):
        delta = timedelta(days=d)
        out.append((cur_start - delta, cur_end - delta))
    return out


def to_ms(dt: datetime) -> int:
    """把 datetime 转 Unix epoch milliseconds（按 UTC 解释）。

    底层逻辑：naive datetime.timestamp() 按 OS 本地时区解释，会导致
    UTC datetime 被当作 BJT → 时间窗口偏 8h，所有 SHoW 对比都错。
    必须显式打 tzinfo=UTC。
    """
    from datetime import timezone as _tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return int(dt.timestamp() * 1000)
