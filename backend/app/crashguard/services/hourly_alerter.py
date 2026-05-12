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


async def _upsert_snapshot(
    session, issue_id: str, hour_utc: datetime, events: int,
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
        ))
    else:
        existing.events_count = events


async def _resolve_baseline(
    session, issue_id: str, window_start: datetime, min_baseline: int,
) -> tuple[Optional[float], str]:
    """优先 SHoW-3h（7 天前同 weekday 同 3h 块）；不足回落 rolling 过去 7 天同名块均值。

    返回 (baseline_or_None, source: 'show' | 'rolling_7d' | 'insufficient')。
    """
    show_target = window_start - timedelta(days=7)
    show_row = (await session.execute(
        select(CrashHourlySnapshot).where(
            CrashHourlySnapshot.datadog_issue_id == issue_id,
            CrashHourlySnapshot.hour_utc == show_target,
        )
    )).scalars().first()
    if show_row is not None and show_row.events_count >= min_baseline:
        return float(show_row.events_count), "show"

    # Fallback: rolling 过去 7 天同名 3h 块均值（共最多 7 个值）
    # 同名块 = window_start 的 hour 字段相同（00/03/06/...）
    target_hour = window_start.hour
    fallback_start = window_start - timedelta(days=7)
    rolling = (await session.execute(
        select(CrashHourlySnapshot.events_count).where(
            CrashHourlySnapshot.datadog_issue_id == issue_id,
            CrashHourlySnapshot.hour_utc >= fallback_start,
            CrashHourlySnapshot.hour_utc < window_start,
        )
    )).all()
    if not rolling:
        return None, "insufficient"
    # 仅保留 hour 字段匹配的同名块（hour_utc 是 datetime，需要拉出来对比）
    same_block_rows = (await session.execute(
        select(CrashHourlySnapshot.events_count, CrashHourlySnapshot.hour_utc).where(
            CrashHourlySnapshot.datadog_issue_id == issue_id,
            CrashHourlySnapshot.hour_utc >= fallback_start,
            CrashHourlySnapshot.hour_utc < window_start,
        )
    )).all()
    same_block = [r[0] for r in same_block_rows if r[1].hour == target_hour]
    if not same_block:
        # 同名块也没数据 → 退到全部 rolling 均值（更宽容兜底）
        vals = [r[0] for r in rolling]
        avg = sum(vals) / len(vals)
    else:
        avg = sum(same_block) / len(same_block)
    if avg < min_baseline:
        return None, "insufficient"
    return avg, "rolling_7d"


async def run_hourly_alert_tick(
    *, force: bool = False, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """单次告警闭环。返回 logging dict。

    force=True 时跳过 idempotency 检查（dev 调试用）。
    now 可注入，便于单测。
    """
    s = get_crashguard_settings()
    if not s.enabled or not s.feishu_enabled:
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

    if not raw_issues:
        logger.info("hourly_alerter: no events in window %s ~ %s", window_start, window_end)
        return {"ok": True, "alerted": False, "reason": "no_events",
                "hour_utc": now_hour.isoformat()}

    new_items: List[Dict[str, Any]] = []
    surge_items: List[Dict[str, Any]] = []
    new_window_cutoff = now_hour - timedelta(days=s.hourly_alert_new_window_days)
    threshold_ratio = s.hourly_alert_growth_threshold_pct / 100.0
    min_baseline = s.hourly_alert_min_baseline_events
    min_sessions = int(getattr(s, "hourly_alert_min_sessions", 60) or 0)

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
            await _upsert_snapshot(session, issue_id, window_start, events_h)

            # 绝对量级阈值过滤：sessions 太低 → 噪声，跳过告警判定（snapshot 已入库不影响 SHoW）
            if min_sessions > 0 and sessions_h < min_sessions:
                continue

            # 查 issue 元信息
            issue_row = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == issue_id)
            )).scalars().first()
            title = (issue_row.title if issue_row else "") or (attrs.get("title") or "") or issue_id
            platform = (issue_row.platform if issue_row else "") or (attrs.get("platform") or "")

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

            # SHoW baseline
            baseline, baseline_source = await _resolve_baseline(
                session, issue_id, window_start, min_baseline,
            )
            if baseline is None or baseline <= 0:
                continue
            growth = (events_h - baseline) / baseline
            if growth >= threshold_ratio:
                surge_items.append({
                    "issue_id": issue_id,
                    "title": title[:100],
                    "platform": platform,
                    "events_h": events_h,
                    "sessions_h": sessions_h,
                    "baseline": round(baseline, 1),
                    "growth_pct": round(growth * 100, 1),
                    "baseline_source": baseline_source,
                })
        await session.commit()

    # 按事件量 desc 排序
    new_items.sort(key=lambda x: x["events_h"], reverse=True)
    surge_items.sort(key=lambda x: x["growth_pct"], reverse=True)

    if not new_items and not surge_items:
        return {
            "ok": True, "alerted": False, "reason": "no_anomaly",
            "hour_utc": now_hour.isoformat(),
            "total_issues_seen": len(raw_issues),
        }

    # === 先记账拿 alert_id：卡片 URL 要用 ID 做深链跳转 ===
    payload = {
        "new": new_items, "surge": surge_items,
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

    # === 构造并发送 feishu 卡片（URL 带 alert_id，点击直接打开 reports 页对应详情）===
    from app.crashguard.services.feishu_card import build_hourly_alert_card
    card = build_hourly_alert_card(
        hour_utc=now_hour,
        new_items=new_items[: s.hourly_alert_max_items],
        surge_items=surge_items[: s.hourly_alert_max_items],
        threshold_pct=s.hourly_alert_growth_threshold_pct,
        frontend_base_url=s.frontend_base_url,
        alert_id=alert_id,
    )

    sent_ok = False
    try:
        from app.services.feishu_cli import send_interactive_card
        if s.feishu_target_chat_id:
            sent_ok = await send_interactive_card(
                chat_id=s.feishu_target_chat_id, card=card,
            )
        elif s.feishu_target_email:
            sent_ok = await send_interactive_card(
                email=s.feishu_target_email, card=card,
            )
        else:
            logger.warning("hourly_alerter: no chat_id/email configured, skip send")
    except Exception:
        logger.exception("hourly_alerter: feishu send error")

    logger.info(
        "hourly_alerter fired: hour=%s new=%d surge=%d sent=%s",
        now_hour.isoformat(), len(new_items), len(surge_items), sent_ok,
    )
    return {
        "ok": True, "alerted": True, "sent": sent_ok,
        "new": len(new_items), "surge": len(surge_items),
        "hour_utc": now_hour.isoformat(),
    }
