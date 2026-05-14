"""
3-hour crash alerter — SHoW-3h（Same 3-Hour-Block of Week）窗口对比。

底层逻辑：Plaud app 工作日 vs 非工作日活跃用户数差异大；1 小时颗粒度噪声大、易假警，
3 小时块平滑分钟级波动 + 仍能在 < 1 个工作时段内捕捉异常。基线用「上周同 weekday
同 3h 块」，控 weekday + 时区双重周期。

3h 块对齐：UTC 整点向下 floor 到 00 / 03 / 06 / 09 / 12 / 15 / 18 / 21。

闭环:
  1. cron 每 3 小时第 5 分钟触发（让 Datadog ingest 稳定）
  2. 拉 [now_block - 3h, now_block] 窗口 fatal events；每 issue 写 crash_hourly_snapshots
     （表名沿用 hourly 字面，含义已是 3h 块；存的 hour_utc 是块起点）
  3. 分类:
     - 新增 → first_seen_at 在过去 N 天内（默认 30d）或 DB 不存在
     - 上涨 → 本块 events vs SHoW-3h 基线 > 阈值（默认 10%）
     - SHoW 缺失 → 回落 rolling 过去 7 天同名块均值
  4. 聚合 digest 飞书卡片，复用早晚报群与卡片样式
  5. CrashHourlyAlert UNIQUE(hour_utc) 防同块重发；多机部署 DB 抢锁兜底

🚫 严禁包含 PR 修复内容——卡片只展示"出了什么事"，PR 状态查看走前端。
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import (
    CrashHourlyAlert,
    CrashHourlySnapshot,
    CrashIssue,
)
from app.crashguard.services.datadog_cache import DatadogCache
from app.crashguard.services.version_classifier import classify_version
from app.db.database import get_session

logger = logging.getLogger("crashguard.hourly_alerter")


# 3h 块步长（小时）
BLOCK_HOURS = 3


def _floor_to_block(dt: datetime) -> datetime:
    """对齐到 3h 块起点：00/03/06/09/12/15/18/21 UTC。"""
    hour = (dt.hour // BLOCK_HOURS) * BLOCK_HOURS
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


# 旧名兼容（测试/外部引用可能 import）
_floor_to_hour = _floor_to_block


def _parse_first_seen(raw) -> Optional[datetime]:
    """解析 Datadog API 返回的 first_seen 字段（兼容 ISO string / unix ms / datetime）。"""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if isinstance(raw, (int, float)):
        # unix ms
        return datetime.utcfromtimestamp(raw / 1000)
    if isinstance(raw, str):
        # ISO 8601 string，去 Z 后缀和 timezone
        s = raw.replace("Z", "").split("+")[0].split(".")[0]
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


async def _fetch_hourly_events(
    window_start: datetime, window_end: datetime,
) -> List[Dict[str, Any]]:
    """拉过去 3 小时窗口的 fatal events。失败抛异常，由 caller 兜。"""
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        logger.warning("hourly_alerter: datadog_api_key not configured, skip")
        return []
    from app.crashguard.services.datadog_client import DatadogClient
    client = DatadogClient(
        api_key=s.datadog_api_key,
        app_key=s.datadog_app_key,
        site=s.datadog_site,
    )
    start_ms = int(window_start.timestamp() * 1000)
    end_ms = int(window_end.timestamp() * 1000)
    return await client.list_issues_for_window(
        start_ms=start_ms,
        end_ms=end_ms,
        tracks=s.datadog_tracks,
        query=s.datadog_query_fatal or s.datadog_query or "*",
        use_cache=False,  # 告警走实时口径，不走缓存
    )


async def _fetch_24h_events(now: datetime) -> List[Dict[str, Any]]:
    """拉过去 24h 累计 events for 通道 3 (新 crash 兜底)。走 DatadogCache TTL 6h。

    复用 _fetch_hourly_events 同款 DatadogClient.list_issues_for_window，仅窗口改 24h。
    """
    async def _do_fetch() -> List[Dict[str, Any]]:
        s = get_crashguard_settings()
        if not s.datadog_api_key:
            return []
        from app.crashguard.services.datadog_client import DatadogClient
        client = DatadogClient(
            api_key=s.datadog_api_key, app_key=s.datadog_app_key, site=s.datadog_site,
        )
        window_end = now
        window_start = now - timedelta(hours=24)
        return await client.list_issues_for_window(
            start_ms=int(window_start.timestamp() * 1000),
            end_ms=int(window_end.timestamp() * 1000),
            tracks=s.datadog_tracks,
            query=s.datadog_query_fatal or s.datadog_query or "*",
            use_cache=False,
        )

    return await DatadogCache.get_or_fetch(
        key="hourly_alert:new_crash:24h",
        ttl_seconds=6 * 3600,
        fetch_fn=_do_fetch,
    )


async def _upsert_snapshot(
    session, issue_id: str, hour_utc: datetime, events: int, sessions: int = 0,
) -> None:
    existing = (await session.execute(
        select(CrashHourlySnapshot).where(
            CrashHourlySnapshot.datadog_issue_id == issue_id,
            CrashHourlySnapshot.hour_utc == hour_utc,
        )
    )).scalars().first()
    if existing is None:
        session.add(CrashHourlySnapshot(
            datadog_issue_id=issue_id,
            hour_utc=hour_utc,
            events_count=events,
            sessions_count=sessions,
        ))
    else:
        existing.events_count = events
        existing.sessions_count = sessions


async def _fallback_sessions_baseline(
    session, issue_id: str, window_start: datetime,
    days: int = 14, min_samples: int = 3,
) -> Optional[float]:
    """SHoW 历史 snapshot 无 sessions_count（老数据）时的兜底：
    取该 issue 过去 N 天所有 sessions_count > 0 的 snapshot 中位数。

    抓手：sessions_count 列 5/14 才上线，SHoW=上周同 3h 块基本无 sessions；
    没有这个兜底，rate-AND-check 退化为纯 events% 单闸门——P0 严格化后将无任何 surge 告警。
    """
    base_from = window_start - timedelta(days=days)
    rows = (await session.execute(
        select(CrashHourlySnapshot.sessions_count).where(
            CrashHourlySnapshot.datadog_issue_id == issue_id,
            CrashHourlySnapshot.hour_utc >= base_from,
            CrashHourlySnapshot.hour_utc < window_start,
            CrashHourlySnapshot.sessions_count > 0,
        )
    )).all()
    samples = sorted([int(r[0]) for r in rows if r[0] and r[0] > 0])
    if len(samples) < min_samples:
        return None
    return float(samples[len(samples) // 2])  # median


async def _resolve_baseline(
    session, issue_id: str, window_start: datetime, min_baseline: int,
) -> tuple[Optional[float], Optional[float], str]:
    """优先 SHoW-3h（7 天前同 weekday 同 3h 块）；不足回落 rolling 过去 7 天同名块均值。

    返回 (events_baseline, sessions_baseline, source)。
    events_baseline=None → 数据不足；sessions_baseline=None → 老 snapshot 无 sessions_count。
    """
    show_target = window_start - timedelta(days=7)
    # 用区间而非 `==`：防御文本存储下的格式漂移（19 vs 26 字符 microseconds）。
    show_row = (await session.execute(
        select(CrashHourlySnapshot).where(
            CrashHourlySnapshot.datadog_issue_id == issue_id,
            CrashHourlySnapshot.hour_utc >= show_target,
            CrashHourlySnapshot.hour_utc < show_target + timedelta(seconds=1),
        )
    )).scalars().first()
    if show_row is not None and show_row.events_count >= min_baseline:
        sess_b = float(show_row.sessions_count) if (show_row.sessions_count or 0) > 0 else None
        return float(show_row.events_count), sess_b, "show"

    # Fallback: rolling 过去 7 天同名 3h 块均值
    target_hour = window_start.hour
    fallback_start = window_start - timedelta(days=7)
    rows = (await session.execute(
        select(
            CrashHourlySnapshot.events_count,
            CrashHourlySnapshot.sessions_count,
            CrashHourlySnapshot.hour_utc,
        ).where(
            CrashHourlySnapshot.datadog_issue_id == issue_id,
            CrashHourlySnapshot.hour_utc >= fallback_start,
            CrashHourlySnapshot.hour_utc < window_start,
        )
    )).all()
    if not rows:
        return None, None, "insufficient"
    same_block = [(e, s) for (e, s, hu) in rows if hu.hour == target_hour]
    pool = same_block if same_block else [(e, s) for (e, s, _) in rows]
    evs_avg = sum(e for e, _ in pool) / len(pool)
    sess_pool = [s for _, s in pool if (s or 0) > 0]
    sess_avg = (sum(sess_pool) / len(sess_pool)) if sess_pool else None
    if evs_avg < min_baseline:
        return None, None, "insufficient"
    return evs_avg, sess_avg, "rolling_7d"


async def run_hourly_alert_tick(
    *, force: bool = False, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """单次告警闭环。返回 logging dict。

    force=True 时跳过 idempotency 检查（dev 调试用）。
    now 可注入，便于单测。
    """
    s = get_crashguard_settings()
    # feishu_enabled 拆为「发送」开关，不杀整条 tick；本地 dev 关飞书仍 upsert snapshot。
    if not s.enabled:
        return {"skipped": "kill_switch", "enabled": s.enabled, "feishu": s.feishu_enabled}
    if not s.hourly_alert_enabled:
        return {"skipped": "hourly_alert_disabled"}

    now = now or datetime.utcnow()
    now_block = _floor_to_block(now)
    window_start = now_block - timedelta(hours=BLOCK_HOURS)
    window_end = now_block
    # snapshot.hour_utc = 数据时段起点（与上周同块 SHoW 查找对齐）
    # alert.hour_utc    = 当前块边界 now_block（UNIQUE 防同块重发；cron 每 3h 触发对齐）
    now_hour = now_block

    # === Idempotency: 同 hour_utc 不重发 ===
    if not force:
        async with get_session() as session:
            existing = (await session.execute(
                select(CrashHourlyAlert).where(CrashHourlyAlert.hour_utc == now_hour)
            )).scalars().first()
            if existing is not None:
                return {
                    "skipped": "already_alerted",
                    "hour_utc": now_hour.isoformat(),
                    "feishu_message_id": existing.feishu_message_id,
                }

    # === Fetch Datadog ===
    try:
        raw_issues = await _fetch_hourly_events(window_start, window_end)
    except Exception as exc:
        logger.exception("hourly_alerter: datadog fetch failed")
        return {"ok": False, "error": f"datadog: {exc}", "hour_utc": now_hour.isoformat()}

    if not raw_issues and not s.hourly_alert_new_crash_enabled:
        logger.info("hourly_alerter: no events in window %s ~ %s", window_start, window_end)
        return {"ok": True, "alerted": False, "reason": "no_events",
                "hour_utc": now_hour.isoformat()}

    # === 获取各平台「用户量最大版本」（通道 1 分桶依据）===
    top_versions: Dict[str, Any] = {}
    if s.hourly_alert_new_version_enabled:
        try:
            from app.crashguard.services.datadog_client import DatadogClient
            _ddclient = DatadogClient(
                api_key=s.datadog_api_key,
                app_key=s.datadog_app_key,
                site=s.datadog_site,
            )
            top_versions = await DatadogCache.get_or_fetch(
                "top_user_version:24",
                ttl_seconds=6 * 3600,
                fetch_fn=lambda: _ddclient.top_user_version_by_platform(window_hours=24),
            )
        except Exception:
            logger.exception("hourly_alerter: top_user_version fetch failed, fallback to {}")
            top_versions = {}

    new_items: List[Dict[str, Any]] = []
    surge_items: List[Dict[str, Any]] = []
    new_version_items: List[Dict[str, Any]] = []
    new_window_cutoff = now_hour - timedelta(days=s.hourly_alert_new_window_days)
    threshold_ratio = s.hourly_alert_growth_threshold_pct / 100.0
    min_baseline = s.hourly_alert_min_baseline_events
    min_sessions = int(getattr(s, "hourly_alert_min_sessions", 60) or 0)
    # 抓手 #2：events 绝对量级底线（基线低 + 当前 +N% 但绝对增量不痛不痒的伪信号过滤）
    min_events_abs = int(getattr(s, "hourly_alert_min_events_absolute", 0) or 0)
    # 抓手 #1：跨告警去重窗口（小时）—— 同 issue 在 N 小时内已告警过则跳过
    dedup_hours = int(getattr(s, "hourly_alert_dedup_hours", 12) or 0)
    dedup_set: set = set()
    if dedup_hours > 0:
        from app.crashguard.services.alert_dedup import recently_alerted_issue_ids_within_hours
        async with get_session() as _session:
            dedup_set = await recently_alerted_issue_ids_within_hours(_session, hours=dedup_hours)

    async with get_session() as session:
        for raw in raw_issues:
            issue_id = raw.get("id") or ""
            if not issue_id:
                continue
            attrs = raw.get("attributes") or {}
            events_h = int(attrs.get("events_count") or 0)
            sessions_h = int(attrs.get("sessions_affected") or 0)
            if events_h == 0:
                continue

            # 持久化 snapshot（用于下一周的 SHoW 基线）—— snapshot 全量入库，告警阈值另判
            # events + sessions 一起存：rate-AND-check 需要历史 sessions
            await _upsert_snapshot(session, issue_id, window_start, events_h, sessions_h)

            # 绝对量级阈值过滤：sessions 太低 → 噪声，跳过告警判定（snapshot 已入库不影响 SHoW）
            if min_sessions > 0 and sessions_h < min_sessions:
                continue
            # #1 跨告警去重：N 小时内同 issue 已被告警过 → 跳过（防早晚报 + hourly 反复点名）
            if issue_id in dedup_set:
                logger.info(
                    "hourly_alerter: skip issue=%s already alerted in past %dh",
                    issue_id, dedup_hours,
                )
                continue
            # 注意：min_events_absolute 仅约束通道 2（大盘 SHoW 涨幅）；通道 1（新版本桶）
            # 有自己的 new_version_min_events（默认 30）。所以这里**不**应用 min_events_abs，
            # 留到通道 1 之后再卡，否则新版本灰度阶段 events<200 的真问题会被吞掉。

            # 查 issue 元信息
            issue_row = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
            )).scalars().first()
            title = (issue_row.title if issue_row else "") or (attrs.get("title") or "") or issue_id
            platform = (issue_row.platform if issue_row else "") or (attrs.get("platform") or "")

            # === 通道 1：版本分桶 ===
            issue_ver = attrs.get("version") or (issue_row.last_seen_version if issue_row else "") or ""
            bucket = classify_version(issue_ver, platform.lower(), top_versions)
            if bucket == "new" and s.hourly_alert_new_version_enabled:
                denom = (top_versions.get(platform.lower()) or {}).get("users") or 0
                user_rate = events_h / denom if denom > 0 else None
                if (
                    events_h >= s.hourly_alert_new_version_min_events
                    and user_rate is not None
                    and user_rate >= s.hourly_alert_new_version_user_rate_pct
                ):
                    new_version_items.append({
                        "issue_id": issue_id,
                        "title": title[:100],
                        "platform": platform,
                        "version": issue_ver,
                        "first_seen_version": (issue_row.first_seen_version if issue_row else "") or "",
                        "events_h": events_h,
                        "sessions_h": sessions_h,
                        "user_rate_pct": round((user_rate or 0) * 100, 3),
                    })
                continue  # 新版本桶：无论是否触发，不走通道 2 逻辑

            # === 通道 2 入口：先卡 events 绝对量级底线（通道 1 已绕过）===
            # #2 events 绝对量级底线：events 太低 → 即使百分比涨，业务量级也不痛不痒
            if min_events_abs > 0 and events_h < min_events_abs:
                continue

            # 新增判定：DB 不存在 OR first_seen_at 在 N 天内
            first_seen = issue_row.first_seen_at if issue_row else None
            is_new = (first_seen is None) or (first_seen >= new_window_cutoff)

            if is_new:
                new_items.append({
                    "issue_id": issue_id,
                    "title": title[:100],
                    "platform": platform,
                    "events_h": events_h,
                    "sessions_h": sessions_h,
                    "first_seen": first_seen.isoformat() if first_seen else None,
                })
                continue  # 新增优先，不再算上涨

            # SHoW baseline（events + sessions 两路）
            baseline, sess_baseline, baseline_source = await _resolve_baseline(
                session, issue_id, window_start, min_baseline,
            )
            if baseline is None or baseline <= 0:
                continue
            growth = (events_h - baseline) / baseline
            if growth < threshold_ratio:
                continue

            # P2: SHoW 历史 snapshot 无 sessions_count → 兜底取 14 天中位数
            sess_baseline_source = baseline_source
            if not sess_baseline or sess_baseline <= 0:
                sess_fb = await _fallback_sessions_baseline(
                    session, issue_id, window_start,
                )
                if sess_fb and sess_fb > 0:
                    sess_baseline = sess_fb
                    sess_baseline_source = f"{baseline_source}+sess_fb14d"

            # rate-AND-check（P0 严格化）：rate_base 缺数 → **不告警**（宁缺勿误报）
            # 抓手：之前 rate_base=None 被静默放行 → AND 退化为 OR；
            # 现在强制 AND——历史 sessions 数据不足时也算"信号不足"，不发警。
            rate_now = (events_h / sessions_h) if sessions_h > 0 else None
            rate_base = (baseline / sess_baseline) if sess_baseline and sess_baseline > 0 else None
            if rate_now is None or rate_base is None or rate_base <= 0:
                # 信号不足：放过这一条，写日志便于排查
                logger.info(
                    "hourly_alerter: skip rate-AND-check insufficient: issue=%s events=%s sess=%s base=%s sess_base=%s",
                    issue_id, events_h, sessions_h, baseline, sess_baseline,
                )
                continue
            rate_growth = (rate_now - rate_base) / rate_base
            rate_growth_pct = round(rate_growth * 100, 1)
            # rate 没有同步上涨（流量稀释或持平）→ 用户体验没劣化，不告警
            if rate_growth <= 0.0:
                continue

            surge_items.append({
                "issue_id": issue_id,
                "title": title[:100],
                "platform": platform,
                "events_h": events_h,
                "sessions_h": sessions_h,
                "baseline": round(baseline, 1),
                "sessions_baseline": round(sess_baseline, 1) if sess_baseline else None,
                "growth_pct": round(growth * 100, 1),
                "rate_now": round(rate_now * 100, 3) if rate_now else None,
                "rate_base": round(rate_base * 100, 3) if rate_base else None,
                "rate_growth_pct": rate_growth_pct,
                "baseline_source": sess_baseline_source,
            })
        await session.commit()

    # === 通道 3：全局新 crash 兜底（24h 累计窗口）===
    new_crash_items: List[Dict[str, Any]] = []
    if s.hourly_alert_new_crash_enabled:
        try:
            raw_24h = await _fetch_24h_events(now)
            if not isinstance(raw_24h, list):
                logger.warning("hourly_alerter: new_crash 24h fetch returned non-list, skip")
                raw_24h = []
        except Exception:
            logger.exception("hourly_alerter: new_crash 24h fetch failed")
            raw_24h = []

        new_crash_cutoff = now_hour - timedelta(days=s.hourly_alert_new_window_days)
        new_crash_min_events = s.hourly_alert_new_crash_min_events
        new_crash_min_sessions = s.hourly_alert_new_crash_min_sessions
        async with get_session() as _s24:
            for raw in raw_24h:
                iid = raw.get("id") or ""
                if not iid or iid in dedup_set:
                    continue
                attrs = raw.get("attributes") or {}
                ev24 = int(attrs.get("events_count") or 0)
                ses24 = int(attrs.get("sessions_affected") or 0)
                if ev24 < new_crash_min_events:
                    continue
                if ses24 < new_crash_min_sessions:
                    continue
                # first_seen 判定：优先用 Datadog API 实时 first_seen（避开 pipeline 4h 延迟）
                api_first_seen_raw = (attrs.get("first_seen_timestamp")
                                      or attrs.get("first_seen"))
                api_first_seen = _parse_first_seen(api_first_seen_raw)

                issue_row = (await _s24.execute(
                    select(CrashIssue).where(CrashIssue.datadog_issue_id == iid)
                )).scalars().first()
                db_first_seen = issue_row.first_seen_at if issue_row else None

                first_seen = api_first_seen or db_first_seen   # API 优先
                first_seen_source = ("api" if api_first_seen
                                     else ("db" if db_first_seen else "none"))

                if first_seen is None or first_seen < new_crash_cutoff:
                    continue
                new_crash_items.append({
                    "issue_id": iid,
                    "title": ((issue_row.title if issue_row else None) or
                              attrs.get("title") or iid)[:100],
                    "platform": (issue_row.platform if issue_row else "") or
                                (attrs.get("platform") or ""),
                    "first_seen_version": (issue_row.first_seen_version
                                           if issue_row else "") or "",
                    "first_seen_at": first_seen.isoformat() if first_seen else None,
                    "first_seen_source": first_seen_source,
                    "events_24h": ev24,
                    "sessions_24h": ses24,
                })

    # === 多通道合卡 dedup：优先级 new_version > new_crash > new > surge ===
    _seen_ids: set = set()
    _dedup = lambda items: [it for it in items
                             if it["issue_id"] not in _seen_ids
                             and not _seen_ids.add(it["issue_id"])]
    new_version_items = _dedup(new_version_items)
    new_crash_items = _dedup(new_crash_items)
    new_items = _dedup(new_items)
    surge_items = _dedup(surge_items)

    # === Shadow mode 判定 ===
    # 只有 new_version / new_crash 通道有命中，且全部处于 shadow_mode → 跳过飞书发送
    def _is_shadow_only(items: list, shadow_flag: bool) -> bool:
        return (not items) or shadow_flag

    has_any_hit = bool(new_version_items or new_crash_items or new_items or surge_items)
    shadow_mode_active = (
        has_any_hit
        and (not new_items)
        and (not surge_items)
        and _is_shadow_only(new_version_items, s.hourly_alert_new_version_shadow_mode)
        and _is_shadow_only(new_crash_items, s.hourly_alert_new_crash_shadow_mode)
    )

    # 按事件量 desc 排序
    new_items.sort(key=lambda x: x["events_h"], reverse=True)
    surge_items.sort(key=lambda x: x["growth_pct"], reverse=True)

    if not new_items and not surge_items and not new_version_items and not new_crash_items:
        return {
            "ok": True, "alerted": False, "reason": "no_anomaly",
            "hour_utc": now_hour.isoformat(),
            "total_issues_seen": len(raw_issues),
        }

    # === 先记账拿 alert_id：卡片 URL 要用 ID 做深链跳转 ===
    payload = {
        "new": new_items, "surge": surge_items, "new_version": new_version_items,
        "new_crash": new_crash_items,
        "threshold_pct": s.hourly_alert_growth_threshold_pct,
        "min_sessions": min_sessions,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }
    alert_id: Optional[int] = None
    try:
        async with get_session() as session:
            row = CrashHourlyAlert(
                hour_utc=now_hour,
                new_count=len(new_items),
                surge_count=len(surge_items),
                feishu_message_id="",
                alert_payload=_json.dumps(payload, ensure_ascii=False, default=str),
            )
            session.add(row)
            await session.commit()
            alert_id = row.id
    except Exception:
        logger.exception("hourly_alerter: alert row insert failed (likely race; ignored)")

    # === Shadow mode 短路：仅 shadow 通道有命中 → 跳过飞书，audit log 已写 ===
    if shadow_mode_active:
        logger.info(
            "hourly_alerter: shadow_mode active (only new_version/new_crash channels in shadow), skip feishu send"
        )
        return {
            "ok": True, "alerted": False, "shadow": True,
            "new": len(new_items), "surge": len(surge_items),
            "new_version": len(new_version_items),
            "new_crash": len(new_crash_items),
            "hour_utc": now_hour.isoformat(),
        }

    # === 构造并发送 feishu 卡片（URL 带 alert_id，点击直接打开 reports 页对应详情）===
    from app.crashguard.services.feishu_card import build_hourly_alert_card
    card = build_hourly_alert_card(
        hour_utc=now_hour,
        new_items=new_items[: s.hourly_alert_max_items],
        surge_items=surge_items[: s.hourly_alert_max_items],
        new_version_items=new_version_items[: s.hourly_alert_max_items],
        new_crash_items=new_crash_items[: s.hourly_alert_max_items],
        threshold_pct=s.hourly_alert_growth_threshold_pct,
        frontend_base_url=s.frontend_base_url,
        alert_id=alert_id,
    )

    sent_ok = False
    if not s.feishu_enabled:
        logger.info("hourly_alerter: feishu_enabled=False, skip send (snapshot 已落表)")
    else:
        # 路由：feishu_alert_email（点对点）> chat_id（群）> feishu_target_email（兼容旧路径）
        # 早晚报继续走 chat_id 进群；hourly_alert 默认走 alert_email 不打扰群里其他人
        try:
            from app.services.feishu_cli import send_interactive_card
            if s.feishu_alert_email:
                sent_ok = await send_interactive_card(
                    email=s.feishu_alert_email, card=card,
                )
            elif s.feishu_target_chat_id:
                sent_ok = await send_interactive_card(
                    chat_id=s.feishu_target_chat_id, card=card,
                )
            elif s.feishu_target_email:
                sent_ok = await send_interactive_card(
                    email=s.feishu_target_email, card=card,
                )
            else:
                logger.warning("hourly_alerter: no alert_email/chat_id configured, skip send")
        except Exception:
            logger.exception("hourly_alerter: feishu send error")

    logger.info(
        "hourly_alerter fired: hour=%s new=%d surge=%d nv=%d nc=%d sent=%s",
        now_hour.isoformat(), len(new_items), len(surge_items), len(new_version_items),
        len(new_crash_items), sent_ok,
    )
    return {
        "ok": True, "alerted": True, "sent": sent_ok,
        "new": len(new_items), "surge": len(surge_items), "new_version": len(new_version_items),
        "new_crash": len(new_crash_items),
        "hour_utc": now_hour.isoformat(),
    }
