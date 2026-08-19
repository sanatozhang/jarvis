"""Graygate「Top 5 崩溃 + Top 5 卡顿」—— 不看是否新增，按 events 量取当前
（`config.version_pattern` 范围内）最大的几个问题，跟「新增崩溃」互补：
新增看变化（find_new_crashes），Top N 看存量大小。

崩溃：复用 crashguard `DatadogClient.list_issues_for_window`（Error Tracking
issue search，纯只读，不碰 `crash_*` 表，同 new_crashes.py 的复用方式）。

卡顿：jank 不是 Error Tracking issue，是纯 Logs 事件（crashguard 自己的
`jank_ingester.py` 头注释已说明这一点，它把 jank 摄入进 `crash_issues` 表做
符号化用）。本模块**不读那张表**（避免碰 crash_* 表），改为独立调用
`DatadogClient.search_logs_page` 直接拉 Datadog Logs，自己按
(platform, 聚合 label) 分组计数——比 jank_ingester 的符号化聚合键粗糙得多，
但本模块只需要"哪个位置卡顿最多"这一个粗粒度信号，不需要跨版本符号稳定聚合。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

from app.crashguard.services.datadog_client import CircuitBreakerOpen, DatadogClient, normalize_issue
from app.crashguard.services.version_util import service_filter_for_generation
from app.graygate.config import get_graygate_settings

logger = logging.getLogger("graygate.top_issues")

_BJT = ZoneInfo("Asia/Shanghai")
_TOP_N = 5
_JANK_MAX_PAGES = 20  # 安全上限（100 events/页），防止卡顿事件量极大时无限翻页


@dataclass
class TopCrash:
    platform: str        # "ios" / "android"
    events_count: int
    title: str
    datadog_url: str
    # 注意：没有 version 字段——issue 的 last_seen_version 是整个生命周期最后一次
    # 出现的版本，不是"为什么被 version_pattern 过滤器捞进来"的那个版本（issue 本身
    # 确实在 4.0.3* 上发生过，但 last_seen_version 可能是更早/更新的版本，展示出来
    # 容易让人误以为报表串了版本范围——2026-08-19 真实数据验证时发现，直接去掉更诚实）


@dataclass
class TopJank:
    platform: str
    label: str            # 聚合位置标签（app_stack_frame / module+offset / module.symbol）
    events_count: int


def _window_ms(target_date: date) -> Tuple[int, int]:
    start_bjt = datetime.combine(target_date, time.min, tzinfo=_BJT)
    end_bjt = start_bjt + timedelta(days=1)
    return int(start_bjt.timestamp() * 1000), int(end_bjt.timestamp() * 1000)


def _client() -> DatadogClient:
    settings = get_graygate_settings()
    return DatadogClient(
        api_key=settings.datadog_api_key,
        app_key=settings.datadog_app_key,
        site=settings.datadog_site,
        service_filter=service_filter_for_generation("native", ""),
    )


async def find_top_crashes(target_date: date) -> List[TopCrash]:
    """按 events_count 降序取 Top 5 现有崩溃（不筛选是否新增，version_pattern 范围内）。

    Datadog 查询失败（HTTP 错误 / CircuitBreakerOpen）→ 返回空 list，不抛异常。
    """
    settings = get_graygate_settings()
    start_ms, end_ms = _window_ms(target_date)
    client = _client()
    try:
        issues_raw = await client.list_issues_for_window(
            start_ms, end_ms, query=f"version:{settings.version_pattern}",
        )
    except CircuitBreakerOpen as e:
        logger.warning("top_issues: crash query circuit breaker open: %s", e)
        return []
    except Exception as e:
        logger.warning("top_issues: crash query failed: %s", e)
        return []

    issues = [normalize_issue(r) for r in issues_raw]
    out = [
        TopCrash(
            platform=(i.get("platform") or "").lower(),
            events_count=i.get("events_count") or 0,
            title=i.get("title") or "",
            datadog_url=(
                f"https://app.{settings.datadog_site}/error-tracking/issue/"
                f"{i.get('datadog_issue_id', '')}"
            ),
        )
        for i in issues
    ]
    out.sort(key=lambda c: c.events_count, reverse=True)
    return out[:_TOP_N]


def _jank_label(attrs: dict) -> str:
    """从 jank log event 的 attributes 里提取一个粗粒度可读位置标签。"""
    if attrs.get("has_app_frame"):
        frame = (attrs.get("app_stack_frame") or "").strip()
        if frame:
            return frame
        module = (attrs.get("app_stack_module") or "").strip()
        offset = (attrs.get("app_stack_module_offset") or "").strip()
        if module:
            return f"{module}+{offset}" if offset else module
    top_module = (attrs.get("stack_top_module") or "").strip()
    top_symbol = (attrs.get("stack_top_symbol") or "").strip()
    label = f"{top_module}.{top_symbol}".strip(".")
    return label or "unknown"


async def find_top_jank(target_date: date) -> List[TopJank]:
    """按事件量降序取 Top 5 卡顿位置（platform + 聚合 label 分组计数）。

    事件量过大时按 `_JANK_MAX_PAGES` 截断（记日志说明截断这件事，不静默丢弃）。
    Datadog 查询失败（HTTP 错误 / CircuitBreakerOpen）→ 返回空 list，不抛异常。
    """
    settings = get_graygate_settings()
    start_ms, end_ms = _window_ms(target_date)
    client = _client()
    query = f"@category:performance jank_watchdog_block version:{settings.version_pattern}"

    counts: Dict[Tuple[str, str], int] = {}
    cursor = None
    pages = 0
    try:
        while True:
            page = await client.search_logs_page(
                query=query, from_ms=start_ms, to_ms=end_ms, cursor=cursor, limit=100,
            )
            for event in page.get("data") or []:
                attrs = ((event or {}).get("attributes") or {}).get("attributes") or {}
                os_info = attrs.get("os") or {}
                platform = (
                    (os_info.get("name") or "").strip().lower()
                    if isinstance(os_info, dict) else ""
                )
                if not platform:
                    continue
                key = (platform, _jank_label(attrs))
                counts[key] = counts.get(key, 0) + 1
            cursor = page.get("next_cursor")
            pages += 1
            if not cursor:
                break
            if pages >= _JANK_MAX_PAGES:
                logger.warning(
                    "top_issues: jank log pagination truncated at %d pages "
                    "(~%d events) — top-5 排名可能不完整", pages, pages * 100,
                )
                break
    except CircuitBreakerOpen as e:
        logger.warning("top_issues: jank query circuit breaker open: %s", e)
        return []
    except Exception as e:
        logger.warning("top_issues: jank query failed: %s", e)
        return []

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_N]
    return [TopJank(platform=k[0], label=k[1], events_count=v) for k, v in ranked]
