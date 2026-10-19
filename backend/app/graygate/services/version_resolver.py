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

⚠️ 字段名叫 `*_events`，但查询已加 `@type:session` 过滤，实际是**会话（session）数**，
不是全部类型 RUM 事件数——2026-08-19 用户发现卡片上显示的"177,496 events"大得
不合理才核实出来：最初这里没加 `@type:` 过滤，count 统计的是 view/action/resource/
error 混在一起的全部事件，同一个 build 真实 session 数其实只有 1629，量级差 100 倍。
字段名保留 `_events` 是历史命名，没有跟着改（避免牵动 report_builder.py/card_builder.py
里已有的引用），但语义上请当作"会话数"读。
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
    versions: List[Tuple[str, int]] = field(default_factory=list)  # 按 events(session) 降序
    top_version: Optional[str] = None       # session 数最大的 build（自动判定的"主力包"）
    top_version_events: int = 0
    total_events: int = 0
    # RUM 采样还原（2026-10-19）：上面的 *_events 是 Datadog 里看到的**采样内**会话数，
    # 样本地板 / 主力包判定继续用它（真实样本量）；展示给人看的会话数用下面的估算全量
    # = Σ n × 100 / session_sample_rate（按版本逐桶，rate 是按 build 配的）。
    estimated: Dict[str, int] = field(default_factory=dict)   # version → 估算全量会话数
    sample_rates: Dict[str, float] = field(default_factory=dict)  # version → 该版本最低采样率

    def est_sessions(self, version: Optional[str] = None) -> int:
        """单版本（或 version=None 时全部版本）的估算全量会话数；没有还原数据时回落采样内数。"""
        if version is None:
            return sum(self.estimated.get(v, n) for v, n in self.versions)
        return self.estimated.get(version, dict(self.versions).get(version, 0))

    def sample_rate(self, version: Optional[str] = None) -> float:
        """单版本（或全部版本）的最低采样率，100 = 未采样。"""
        if version is None:
            return min(self.sample_rates.values(), default=100.0)
        return self.sample_rates.get(version, 100.0)

    # 2026-08-19：build 号最大的"🆕最新版本"自动层被用户下线——刚发布的包流量太薄，
    # 自动判定意义不大，改成人工指定主要版本（见 services/focus_version.py），
    # 这里不再计算/暴露 newest_version。


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
            # @type:session —— 2026-08-19 修正：原先不带 @type 过滤，count 统计的是
            # 全部类型 RUM 事件（view/action/resource/error 混在一起），量级会比真实
            # session 数大 100 倍以上（实测某 build 事件量 17.7 万 vs 实际 session 数
            # 只有 1629）。用户发现卡片上显示的"events"数字大得不合理，两者一核对
            # 才确认是口径错了。加 @type:session 后取到的才是真正的会话数，
            # min_sessions 样本地板也才名副其实（之前拿 17 万比 50 这个阈值形同虚设）。
            "query": f"@type:session env:production @service:{service} version:{s.version_pattern}",
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

    estimated, sample_rates = await _restore_sampling(
        url, headers, payload, dict(versions), platform,
    )

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
        estimated=estimated,
        sample_rates=sample_rates,
    )


async def _restore_sampling(
    url: str,
    headers: Dict[str, str],
    base_payload: dict,
    sampled_by_version: Dict[str, int],
    platform: str,
) -> Tuple[Dict[str, int], Dict[str, float]]:
    """按 `@_dd.configuration.session_sample_rate` 把采样内会话数还原成估算全量。

    同一个 filter 再按 version × sample_rate 聚合一次。缺这个字段的 session 会被
    Datadog 从分组里丢掉，用「采样内总数 − 有 rate 的部分」补回、按 100% 计。
    查询失败 → 返回空 dict，调用方回落采样内数（等价于假定 100%），不影响日报。
    """
    if not sampled_by_version:
        return {}, {}
    payload = {
        **base_payload,
        "group_by": [
            {"facet": "version", "limit": 30},
            {"facet": "@_dd.configuration.session_sample_rate", "limit": 10},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning("sample-rate aggregate HTTP %s (%s): %s",
                               resp.status_code, platform, resp.text[:300])
                return {}, {}
            buckets = resp.json().get("data", {}).get("buckets", [])
    except Exception as e:
        logger.warning("sample-rate aggregate failed (%s): %s", platform, e)
        return {}, {}

    est: Dict[str, float] = {}
    rated: Dict[str, int] = {}
    rates: Dict[str, float] = {}
    for b in buckets:
        by = b.get("by") or {}
        version = by.get("version")
        if version not in sampled_by_version:
            continue
        try:
            rate = float(by.get("@_dd.configuration.session_sample_rate"))
        except (TypeError, ValueError):
            continue
        n = int(b.get("computes", {}).get("c0") or 0)
        if n <= 0 or not (0 < rate <= 100):
            continue
        est[version] = est.get(version, 0.0) + n * 100.0 / rate
        rated[version] = rated.get(version, 0) + n
        rates[version] = min(rates.get(version, 100.0), rate)

    out: Dict[str, int] = {}
    for version, sampled in sampled_by_version.items():
        unrated = max(0, sampled - rated.get(version, 0))
        out[version] = int(round(est.get(version, 0.0) + unrated))
    return out, rates


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
