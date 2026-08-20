"""共享的、无外部依赖的 db 健康状态存储（2026-08-20）。

被两边同时用：`app/db/database.py`（生产者——捕获真实的 disk I/O 错误）和
`app/services/db_health_monitor.py`（消费者——周期性检查 + 决定要不要告警）。
拆成独立小模块是为了不让 database.py 和 monitor 互相 import 造成循环依赖，
也让 `/api/health` 可以零成本读最近一次检查结果，不用现场触发一次重活。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

# ---- I/O 错误频率 ----
_IO_ERROR_WINDOW_CAP = 500  # 防止极端情况下无限增长；超过这个数早就该告警了
_io_error_times: Deque[float] = deque(maxlen=_IO_ERROR_WINDOW_CAP)


def record_io_error(now: Optional[float] = None) -> None:
    """database.py 的 handle_error 钩子每捕获一次 'disk i/o error' 就调这个。"""
    _io_error_times.append(now if now is not None else time.time())


def count_recent_io_errors(window_seconds: float, now: Optional[float] = None) -> int:
    """过去 window_seconds 内发生了几次（同时把更老的记录清出去，避免队列无限攒旧数据）。"""
    now = now if now is not None else time.time()
    cutoff = now - window_seconds
    while _io_error_times and _io_error_times[0] < cutoff:
        _io_error_times.popleft()
    return len(_io_error_times)


# ---- 告警冷却（同一种告警在冷却窗口内只发一次，避免刷屏） ----
_last_alert_at: dict[str, float] = {}


def should_alert(kind: str, cooldown_seconds: float, now: Optional[float] = None) -> bool:
    """返回 True 时会立刻把 kind 标记成"刚告过"，调用方不需要自己再记时间戳。"""
    now = now if now is not None else time.time()
    last = _last_alert_at.get(kind, 0.0)
    if now - last < cooldown_seconds:
        return False
    _last_alert_at[kind] = now
    return True


# ---- 最近一次检查结果（供 /api/health 读取，不触发新检查） ----
@dataclass
class LastCheck:
    ok: Optional[bool] = None  # None = 还没跑过
    checked_at: Optional[float] = None
    detail: str = ""


@dataclass
class DbHealthSnapshot:
    integrity: LastCheck = field(default_factory=LastCheck)
    journal_mode: LastCheck = field(default_factory=LastCheck)
    recent_io_errors_10min: int = 0


_snapshot = DbHealthSnapshot()


def record_integrity_check(ok: bool, detail: str = "", now: Optional[float] = None) -> None:
    _snapshot.integrity = LastCheck(ok=ok, checked_at=now if now is not None else time.time(), detail=detail)


def record_journal_mode_check(ok: bool, detail: str = "", now: Optional[float] = None) -> None:
    _snapshot.journal_mode = LastCheck(ok=ok, checked_at=now if now is not None else time.time(), detail=detail)


def get_snapshot() -> DbHealthSnapshot:
    """给 /api/health 用：读缓存，不跑新检查。"""
    _snapshot.recent_io_errors_10min = count_recent_io_errors(600)
    return _snapshot
