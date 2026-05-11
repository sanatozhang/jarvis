"""
版本号工具：semver 解析 / 比较 / 从崩溃数据派生"线上最新版本"。

不依赖外部包；处理形如 "3.17.0" / "3.17.0-630" / "v3.17" / "3.17.0+build" 的常见格式。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_SEMVER_RE = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+.](.*))?$"
)


def parse_semver(s: str) -> Optional[Tuple[int, int, int, str]]:
    """解析版本字符串为 (major, minor, patch, suffix)。无法解析返回 None。

    示例：
        "3.17.0"        -> (3, 17, 0, "")
        "3.17"          -> (3, 17, 0, "")
        "3.17.0-630"    -> (3, 17, 0, "630")
        "v3.17.0+build" -> (3, 17, 0, "build")
        "abc"           -> None
    """
    if not s:
        return None
    m = _SEMVER_RE.match(s.strip())
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    suffix = (m.group(4) or "").strip()
    return (major, minor, patch, suffix)


def _sort_key(v: str) -> Tuple[int, int, int, int, str]:
    """排序 key：能解析的优先（用 semver 数值），不能解析的丢到最后（用字典序）。"""
    parsed = parse_semver(v)
    if parsed is None:
        return (0, 0, 0, 0, v)
    major, minor, patch, suffix = parsed
    # 第 1 位 1 表示"可解析"，排在不可解析的（0）之前
    return (1, major, minor, patch, suffix)


def max_version(versions: Iterable[str]) -> str:
    """从一堆版本号中取最大值。空集合返回 ""。"""
    cleaned = [v.strip() for v in versions if v and v.strip()]
    if not cleaned:
        return ""
    return max(cleaned, key=_sort_key)


async def derive_latest_release_from_crashes(
    session: AsyncSession,
    platform: str,
    min_events: int = 300,
) -> str:
    """根据崩溃数据派生"线上最新版本"。

    口径：
      - 拉某平台所有 CrashIssue
      - 以 last_seen_version 为版本桶，按 total_events_across_versions 加权求和
      - 过滤掉 events < min_events 的版本（噪音/灰度版本）
      - 在剩下版本中取 semver 最大值

    返回 "" 表示无法派生（无数据 / 全部低于阈值）。
    """
    from app.crashguard.models import CrashIssue

    if not platform:
        return ""

    # DB 里 platform 可能存大写（ANDROID/IOS）也可能小写（flutter），统一忽略大小写
    stmt = (
        select(CrashIssue.last_seen_version, CrashIssue.total_events)
        .where(func.lower(CrashIssue.platform) == platform.lower())
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return ""

    bucket: dict[str, int] = {}
    for ver, events in rows:
        v = (ver or "").strip()
        if not v:
            continue
        bucket[v] = bucket.get(v, 0) + int(events or 0)

    qualified = [v for v, n in bucket.items() if n >= min_events]
    if not qualified:
        return ""
    return max_version(qualified)


async def resolve_effective_latest_release(
    session: AsyncSession,
    platform: str,
    override: str = "",
    min_events: int = 300,
) -> str:
    """统一入口：优先用配置 override，否则从崩溃数据派生。

    Args:
        platform: flutter / ios / android
        override: 配置里手动指定的最新版本（高优）
        min_events: 派生阈值——版本累计 events 低于此值不考虑（默认 300）

    Returns:
        最新版本字符串，全部失败时返回 ""。
    """
    if override and override.strip():
        return override.strip()
    return await derive_latest_release_from_crashes(
        session=session, platform=platform, min_events=min_events
    )


def collect_recent_versions(
    all_versions: Iterable[str],
    latest: str,
    n: int = 3,
) -> List[str]:
    """从一组版本里取"包括最新版在内的最近 N 个" semver 降序。"""
    parseable: List[Tuple[Tuple[int, int, int, str], str]] = []
    for v in all_versions:
        s = (v or "").strip()
        if not s:
            continue
        p = parse_semver(s)
        if p is None:
            continue
        parseable.append((p, s))
    if not parseable:
        return [latest] if latest else []
    parseable.sort(key=lambda x: x[0], reverse=True)
    seen: List[str] = []
    for _, s in parseable:
        if s not in seen:
            seen.append(s)
        if len(seen) >= n:
            break
    return seen
