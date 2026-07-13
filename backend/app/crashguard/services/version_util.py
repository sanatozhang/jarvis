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


# ── 代际判定（Flutter→native 迁移）─────────────────────────────────
# native 新仓的 Datadog service tag（SDK 盖的最直接真相，2026-06-30 实测，
# 源头见 4.0 代码 DatadogConfig.{kt,swift}）。注意是下划线。
_NATIVE_SERVICES = {"plaud_android", "plaud_ios"}
_FLUTTER_SERVICES = {"plaud-flutter"}
# 版本兜底切线：>=4.0.0 为 native，与 repo_router 的 4.0.0 cutover 一致（单一真相源）。
# 若产品调整切换线，repo_routing config 的 band min_version 与此处需同步。
_NATIVE_MIN_VERSION = (4, 0, 0)

# 代际 badge（行内标注 4.0 native vs 3.x flutter）——daily_report / pr_pending_review_alert 共用。
GEN_BADGE = {"native": "🆕4.0", "flutter": "🦋3.x"}

# 按代际拆分的 Datadog service filter —— 首页"显示3.x"勾选框的单一过滤入口。
# 2026-07-13：发现首页除 issue 列表外，crash-free%/版本分布/机型分布/latest-release
# 全部共用 config.py 里那条混合 filter，勾选框形同虚设。改成在查询源头就选对应
# service 的 filter，下游不用再额外加 group_by 维度去拆——数据从 Datadog 吐出来
# 那一刻就已经是单一代际的了。
_NATIVE_DATADOG_FILTER = "((service:plaud_android AND env:production) OR (service:plaud_ios AND env:production))"
_FLUTTER_DATADOG_FILTER = "(service:plaud-flutter)"


def service_filter_for_generation(generation: str, base_filter: str) -> str:
    """按 generation("native"/"flutter"/"") 选 Datadog service filter。

    generation 为空/未知 → 原样返回 base_filter（向后兼容，等价于"显示全部"）。
    """
    if generation == "native":
        return _NATIVE_DATADOG_FILTER
    if generation == "flutter":
        return _FLUTTER_DATADOG_FILTER
    return base_filter


def classify_generation(service: str = "", version: str = "") -> str:
    """判定崩溃"代际"：'native'（4.0 原生新仓）/ 'flutter'（3.x 旧仓）/ ''（未知）。

    优先级：service tag 为主（plaud_android/plaud_ios=native，plaud-flutter=flutter，
    这是 SDK 直接盖的真相）；service 缺失/非 app 时用 version 兜底（semver>=4.0.0=native，
    与 repo_router 4.0.0 切线一致）；两者都无法判定返回 ''（调用方不标注）。

    示例：
        classify_generation("plaud_ios")            -> "native"
        classify_generation("plaud-flutter")        -> "flutter"
        classify_generation("", "4.0.100")          -> "native"
        classify_generation("", "3.16.0-634")       -> "flutter"
        classify_generation("plaud-web", "")        -> ""   (非 app，不标)
        classify_generation("", "")                 -> ""
    """
    svc = (service or "").strip().lower()
    if svc in _NATIVE_SERVICES:
        return "native"
    if svc in _FLUTTER_SERVICES:
        return "flutter"
    parsed = parse_semver(version or "")
    if parsed is not None:
        return "native" if parsed[:3] >= _NATIVE_MIN_VERSION else "flutter"
    return ""


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


def _generation_allows(service: str, version: str, generation: str) -> bool:
    """判不出代际（service/version 缺失）保守放行，跟其它 generation 过滤口径一致。"""
    if not generation:
        return True
    gen = classify_generation(service, version)
    return (not gen) or gen == generation


async def derive_latest_release_from_crashes(
    session: AsyncSession,
    platform: str,
    min_events: int = 300,
    generation: str = "",
) -> str:
    """根据崩溃数据派生"线上最新版本"。

    口径：
      - 拉某平台所有 CrashIssue
      - 以 last_seen_version 为版本桶，按 total_events_across_versions 加权求和
      - 过滤掉 events < min_events 的版本（噪音/灰度版本）
      - 在剩下版本中取 semver 最大值

    generation: "native"/"flutter"/""（=全部）—— 首页"显示3.x"勾选框透传。

    返回 "" 表示无法派生（无数据 / 全部低于阈值）。
    """
    from app.crashguard.models import CrashIssue

    if not platform:
        return ""

    # DB 里 platform 可能存大写（ANDROID/IOS）也可能小写（flutter），统一忽略大小写
    stmt = (
        select(CrashIssue.last_seen_version, CrashIssue.total_events, CrashIssue.service)
        .where(func.lower(CrashIssue.platform) == platform.lower())
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return ""

    bucket: dict[str, int] = {}
    for ver, events, service in rows:
        v = (ver or "").strip()
        if not v:
            continue
        if not _generation_allows(service or "", v, generation):
            continue
        bucket[v] = bucket.get(v, 0) + int(events or 0)

    qualified = [v for v, n in bucket.items() if n >= min_events]
    if not qualified:
        return ""
    return max_version(qualified)


async def derive_latest_release_candidates(
    session: AsyncSession,
    platform: str,
    min_events: int = 300,
    limit: int = 5,
) -> List[str]:
    """从崩溃数据派生候选最新版本列表（semver 降序，最多 limit 个）。

    与 derive_latest_release_from_crashes 的区别：返回全量候选而非单一最大值，
    供调用方按 session 阈值逐个降级选取。
    """
    from app.crashguard.models import CrashIssue

    if not platform:
        return []

    stmt = (
        select(CrashIssue.last_seen_version, CrashIssue.total_events)
        .where(func.lower(CrashIssue.platform) == platform.lower())
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    bucket: dict[str, int] = {}
    for ver, events in rows:
        v = (ver or "").strip()
        if not v:
            continue
        bucket[v] = bucket.get(v, 0) + int(events or 0)

    qualified = [v for v, n in bucket.items() if n >= min_events]
    if not qualified:
        return []

    qualified.sort(key=_sort_key, reverse=True)
    return qualified[:limit]


async def resolve_effective_latest_release(
    session: AsyncSession,
    platform: str,
    override: str = "",
    min_events: int = 300,
    generation: str = "",
) -> str:
    """统一入口：优先用配置 override，否则从崩溃数据派生。

    Args:
        platform: flutter / ios / android
        override: 配置里手动指定的最新版本（高优）
        min_events: 派生阈值——版本累计 events 低于此值不考虑（默认 300）
        generation: "native"/"flutter"/""（=全部）—— 首页"显示3.x"勾选框透传。
            注意：有 override 时直接生效，不受 generation 过滤（手动配置的值
            视为管理员明确意图，跟"看不看3.x"的视图筛选是两回事）。

    Returns:
        最新版本字符串，全部失败时返回 ""。
    """
    if override and override.strip():
        return override.strip()
    return await derive_latest_release_from_crashes(
        session=session, platform=platform, min_events=min_events, generation=generation,
    )


async def derive_top_user_version_from_crashes(
    session: AsyncSession,
    platform: str,
    generation: str = "",
) -> Optional[dict]:
    """
    fallback：从 crash_issues.top_app_version 字段加权聚合「用户量最大版本」。

    每个 issue 的 top_app_version 格式: "3.16.0-634 (60%), 3.15.1-631 (30%)"
    我们把 (% × total_events) 当 issue 维度的版本影响贡献，跨 issue 求和后取最大。

    ⚠️ 用 `total_events` 而非 `total_users_affected`——后者目前全部为 0
    （Datadog Error Tracking 不直接返回 users，Plan 2.5 RUM Events API 才补，
    见 `models.py:71` 注释）。events 数是用户影响范围的合理代理。

    generation: "native"/"flutter"/""（=全部）—— 按该 issue 的 service + 具体版本串
    联合判定，逐个版本片段过滤，跟首页"显示3.x"勾选框同一口径。

    Returns:
        {"version": str, "users": int} 或 None（无数据）。
        ↑ "users" 字段在 events-代理 口径下实际是加权 events 数。
    """
    from app.crashguard.models import CrashIssue

    if not platform:
        return None

    stmt = (
        select(CrashIssue.top_app_version, CrashIssue.total_events, CrashIssue.service)
        .where(func.lower(CrashIssue.platform) == platform.lower())
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None

    bucket: dict[str, float] = {}
    pattern = re.compile(r"^(.+?)\s*\(([\d.]+)%\)\s*$")
    for top_av, total_events, service in rows:
        top_av = (top_av or "").strip()
        events = int(total_events or 0)
        if not top_av or events <= 0:
            continue
        for part in top_av.split(","):
            m = pattern.match(part.strip())
            if not m:
                continue
            ver = m.group(1).strip()
            try:
                pct = float(m.group(2)) / 100.0
            except ValueError:
                continue
            if not ver or pct <= 0:
                continue
            if not _generation_allows(service or "", ver, generation):
                continue
            bucket[ver] = bucket.get(ver, 0.0) + events * pct

    if not bucket:
        return None
    top_ver, top_events = max(bucket.items(), key=lambda kv: kv[1])
    return {"version": top_ver, "users": int(round(top_events))}


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
