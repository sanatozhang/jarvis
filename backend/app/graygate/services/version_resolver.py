"""Graygate 版本枚举 —— 按平台枚举 `version_pattern` 通配符下实际存在的版本号，
判定每个平台的"主力 build"。

只读实测结论（对 Datadog 现场验证过，详见 task-3-brief.md）：

1. 版本号格式是 `4.0.301-1038`（主版本-build号），不是 semver 三段式，**不做**语义化
   版本比较——排序完全按 events 数（由 Python 端在拿到 buckets 后自己降序排序）。
2. iOS/Android 当前主力 build 不是同一个，**必须按平台分别查询**，不能合并查一次
   再拆分。
3. 请求体如果带 `sort` 字段会 400（实测确认），不传 `sort`。
4. 通配符不能加引号：`version:4.0.3*`（无引号）才有效。

单平台查询失败不抛异常、不影响另一个平台——对齐 `dashboard_query.py::query_scalar`
的容错策略。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from app.graygate.config import get_graygate_settings

logger = logging.getLogger("graygate.version_resolver")

# 对外统一用 "ios" / "android"；HTTP 层用 Datadog 的 @service 值。
_PLATFORM_SERVICE = {
    "ios": "plaud_ios",
    "android": "plaud_android",
}


@dataclass
class PlatformVersions:
    platform: str
    versions: List[Tuple[str, int]] = field(default_factory=list)  # 按 events 降序
    top_version: Optional[str] = None
    top_version_events: int = 0
    total_events: int = 0


def _empty(platform: str) -> PlatformVersions:
    return PlatformVersions(
        platform=platform,
        versions=[],
        top_version=None,
        top_version_events=0,
        total_events=0,
    )


async def _resolve_one_platform(
    platform: str,
    from_ms: int,
    to_ms: int,
) -> PlatformVersions:
    s = get_graygate_settings()
    if not s.datadog_api_key or not s.datadog_app_key:
        logger.warning("datadog keys not configured")
        return _empty(platform)

    service = _PLATFORM_SERVICE[platform]
    url = f"https://api.{s.datadog_site}/api/v2/rum/analytics/aggregate"
    headers = {
        "DD-API-KEY": s.datadog_api_key,
        "DD-APPLICATION-KEY": s.datadog_app_key,
        "Content-Type": "application/json",
    }
    payload = {
        "compute": [{"aggregation": "count", "type": "total"}],
        "filter": {
            "from": from_ms,
            "to": to_ms,
            "query": f"env:production @service:{service} version:{s.version_pattern}",
        },
        "group_by": [{"facet": "version", "limit": 30}],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "version aggregate HTTP %s (%s): %s",
                    resp.status_code, platform, resp.text[:300],
                )
                return _empty(platform)
            buckets = resp.json().get("data", {}).get("buckets", [])
    except Exception as e:
        logger.warning("version aggregate failed (%s): %s", platform, e)
        return _empty(platform)

    versions: List[Tuple[str, int]] = []
    for b in buckets:
        version = (b.get("by") or {}).get("version")
        if not version:
            continue
        events = int(b.get("computes", {}).get("c0") or 0)
        versions.append((version, events))

    # 不带 sort 请求 Datadog（带会 400），在本地按 events 降序排序。
    versions.sort(key=lambda vc: vc[1], reverse=True)

    total_events = sum(events for _, events in versions)
    if versions:
        top_version, top_version_events = versions[0]
    else:
        top_version, top_version_events = None, 0

    return PlatformVersions(
        platform=platform,
        versions=versions,
        top_version=top_version,
        top_version_events=top_version_events,
        total_events=total_events,
    )


async def resolve_versions(
    from_ms: int,
    to_ms: int,
) -> Dict[str, PlatformVersions]:
    """按平台（"ios"、"android"）分别枚举 config.version_pattern 通配符下的实际版本。

    返回 {"ios": PlatformVersions(...), "android": PlatformVersions(...)}。
    单平台查询失败（HTTP 非 200 / 异常）→ 该平台返回空 PlatformVersions，不抛异常、
    不影响另一个平台。
    """
    result: Dict[str, PlatformVersions] = {}
    for platform in ("ios", "android"):
        result[platform] = await _resolve_one_platform(platform, from_ms, to_ms)
    return result
