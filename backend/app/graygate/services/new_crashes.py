"""Graygate「昨日新增崩溃」—— 找出昨日窗口内首次出现、且发生在
`config.version_pattern` 通配符版本上的崩溃堆栈。

只读实测结论（详见 task-4-brief.md）：Datadog `version:4.0.3*` 过滤器过滤的是
「这个 issue 在查询窗口内是否有事件命中该版本模式」，**不是**按 issue 的
`first_seen_version` 字段过滤。实测 25 个命中 issue 里，大多数是老 bug（
`first_seen_version` 是 `4.0.0` / `4.0.100-xxx`）在新版本上偶发复现，不是「新增」。

真正的「昨日新增」必须同时满足两个条件：
1. `first_seen_at` 落在 target_date 当天的 BJT 00:00-24:00 窗口内
2. `first_seen_version` 匹配 `version_pattern`（用 fnmatch）

两个条件缺一不可——只看 Datadog 返回结果会把老 bug 复现误报成新增；只看
first_seen_at 不看 first_seen_version 则可能把窗口内首次出现但版本号其实是
3.x 的 bug 也算进「4.0.3* 新增」。
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List
from zoneinfo import ZoneInfo

from app.crashguard.services.datadog_client import CircuitBreakerOpen, DatadogClient, normalize_issue
from app.crashguard.services.version_util import service_filter_for_generation
from app.graygate.config import get_graygate_settings

logger = logging.getLogger("graygate.new_crashes")

_BJT = ZoneInfo("Asia/Shanghai")
_MAX_RESULTS = 10

# "崩溃"口径——不加这个过滤，Datadog Error Tracking issue search（默认 track=rum）
# 会把所有被分组的 error 都吐出来，包括非致命的捕获异常（网络请求失败、业务层
# NSError 等），跟"崩溃"完全是两回事。2026-08-19 真实数据核实：iOS 昨日事件量
# 最大的"崩溃"其实是一条 11,285 events 的 "Error Domain=test Code=42"，套上这个
# 过滤后完全消失——它根本不是崩溃。照抄 crashguard 自己的口径
# （datadog_client.py::_USER_FATAL_FILTER，Plaud 内部 sessions/告警对齐的定义）：
# 崩溃类含 native crash + ANR + App Hang。
_CRASH_FAMILY_FILTER = '@type:error (@error.is_crash:true OR @error.category:ANR OR @error.category:"App Hang")'


@dataclass
class NewCrash:
    platform: str        # "ios" / "android"（统一小写）
    version: str          # first_seen_version 原样
    events_count: int
    title: str
    datadog_url: str


def _window_ms(target_date: date) -> tuple[int, int]:
    """target_date 当天 BJT 00:00 到次日 00:00，换算成 UTC 毫秒时间戳。"""
    start_bjt = datetime.combine(target_date, time.min, tzinfo=_BJT)
    end_bjt = start_bjt + timedelta(days=1)
    return int(start_bjt.timestamp() * 1000), int(end_bjt.timestamp() * 1000)


async def find_new_crashes(target_date: date) -> List[NewCrash]:
    """返回昨日窗口内新增的崩溃，按 events_count 降序，最多 10 条。

    判定条件（两者都要满足）：
    1. first_seen_at 落在 target_date 当天的 BJT 00:00-24:00 窗口内
    2. first_seen_version 匹配 config.version_pattern（fnmatch）

    Datadog 查询失败（HTTP 错误 / CircuitBreakerOpen）→ 返回空 list，不抛异常
    （报告渲染层据此判断"该段不出"，不是报错）。
    """
    settings = get_graygate_settings()
    start_ms, end_ms = _window_ms(target_date)

    client = DatadogClient(
        api_key=settings.datadog_api_key,
        app_key=settings.datadog_app_key,
        site=settings.datadog_site,
        service_filter=service_filter_for_generation("native", ""),
    )

    try:
        issues_raw = await client.list_issues_for_window(
            start_ms, end_ms,
            query=f"{_CRASH_FAMILY_FILTER} version:{settings.version_pattern}",
        )
    except CircuitBreakerOpen as e:
        logger.warning("new_crashes: datadog circuit breaker open: %s", e)
        return []
    except Exception as e:
        logger.warning("new_crashes: datadog query failed: %s", e)
        return []

    issues = [normalize_issue(r) for r in issues_raw]

    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=ZoneInfo("UTC"))
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=ZoneInfo("UTC"))

    new_crashes: List[NewCrash] = []
    for issue in issues:
        first_seen_at = issue.get("first_seen_at")
        first_seen_version = issue.get("first_seen_version") or ""

        if first_seen_at is None:
            continue
        if not (start_dt <= first_seen_at < end_dt):
            continue
        if not fnmatch.fnmatch(first_seen_version, settings.version_pattern):
            continue

        new_crashes.append(
            NewCrash(
                platform=(issue.get("platform") or "").lower(),
                version=first_seen_version,
                events_count=issue.get("events_count") or 0,
                title=issue.get("title") or "",
                datadog_url=(
                    f"https://app.{settings.datadog_site}/error-tracking/issue/"
                    f"{issue.get('datadog_issue_id', '')}"
                ),
            )
        )

    new_crashes.sort(key=lambda c: c.events_count, reverse=True)
    return new_crashes[:_MAX_RESULTS]
