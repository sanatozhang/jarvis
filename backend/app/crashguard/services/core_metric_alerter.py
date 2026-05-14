"""
核心指标报警（crash-free sessions %）—— 10 分钟粒度，rolling 1h baseline。

底层逻辑：
- 早晚报 = 24h 大盘汇总
- hourly_alert = 单 issue 突增/新增（个体维度）
- core_metric = 整体 crash-free sessions % 健康度（系统维度），即使没有单 issue 飙升，
  整体 crash-free 跌穿基线也能报警

闭环：
  1. cron 每 10 分钟触发（默认 `*/10 * * * *`）
  2. 当前窗口 [now-10min, now] vs 基线 [now-70min, now-10min] 各拉一次 RUM
  3. 算每平台 crash_free_pct 变化（pp = percentage points）
  4. |变化| >= threshold_pp 且 当前 sessions >= min_sessions → 入告警列表
  5. 任一平台触发 → 发飞书卡片；CrashMetricAlert UNIQUE(window_start) 幂等
  6. snapshot 全量入库（用于回看 + 下一 tick 复用基线）

🚫 不含 PR 修复内容；卡片只展示"健康度变化"。
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashMetricAlert, CrashMetricSnapshot
from app.db.database import get_session

logger = logging.getLogger("crashguard.core_metric_alerter")

WINDOW_MINUTES = 10
BASELINE_MINUTES = 60  # 前 1h 平均


def _floor_to_window(dt: datetime) -> datetime:
    """对齐到 10 分钟窗口起点（UTC）。"""
    minute = (dt.minute // WINDOW_MINUTES) * WINDOW_MINUTES
    return dt.replace(minute=minute, second=0, microsecond=0)


async def _fetch_crash_free(start: datetime, end: datetime) -> Dict[str, Dict[str, Any]]:
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        logger.warning("core_metric_alerter: datadog_api_key not configured")
        return {}
    from app.crashguard.services.datadog_client import DatadogClient
    client = DatadogClient(
        api_key=s.datadog_api_key,
        app_key=s.datadog_app_key,
        site=s.datadog_site,
    )
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    return await client.crash_free_sessions_by_platform(start_ms=start_ms, end_ms=end_ms)


async def _upsert_snapshot(
    session, window_start: datetime, platform: str,
    total: int, crashed: int, cf_pct: float,
) -> None:
    existing = (await session.execute(
        select(CrashMetricSnapshot).where(
            CrashMetricSnapshot.window_start == window_start,
            CrashMetricSnapshot.platform == platform,
        )
    )).scalars().first()
    if existing is None:
        session.add(CrashMetricSnapshot(
            window_start=window_start,
            platform=platform,
            total_sessions=total,
            crashed_sessions=crashed,
            crash_free_pct=cf_pct,
        ))
    else:
        existing.total_sessions = total
        existing.crashed_sessions = crashed
        existing.crash_free_pct = cf_pct


async def _baseline_crash_free(
    session, platform: str, current_window_start: datetime,
) -> Optional[float]:
    """前 1h 同平台 snapshot 的加权 crash_free_pct。

    权重 = total_sessions，避开小窗口的极值噪声。
    至少要 2 个有效 snapshot 才返回。
    """
    base_from = current_window_start - timedelta(minutes=BASELINE_MINUTES)
    base_to = current_window_start
    rows = (await session.execute(
        select(CrashMetricSnapshot).where(
            CrashMetricSnapshot.platform == platform,
            CrashMetricSnapshot.window_start >= base_from,
            CrashMetricSnapshot.window_start < base_to,
        )
    )).scalars().all()
    if len(rows) < 2:
        return None
    total_w = sum(int(r.total_sessions or 0) for r in rows)
    if total_w <= 0:
        return None
    weighted = sum(
        float(r.crash_free_pct or 0.0) * int(r.total_sessions or 0)
        for r in rows
    )
    return weighted / total_w


async def run_core_metric_tick(
    *, force: bool = False, now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """单次核心指标 tick；返回 logging dict。

    force=True 跳过 idempotency（dev 调试用）。
    now 可注入便于单测。
    """
    s = get_crashguard_settings()
    # 拆分语义：feishu_enabled 只控发送，不再杀整条 tick。
    # 本地 dev 关掉飞书也能继续算 crash_free % / 写 snapshot。
    if not s.enabled:
        return {"skipped": "kill_switch"}
    if not getattr(s, "core_metric_enabled", False):
        return {"skipped": "core_metric_disabled"}

    now = now or datetime.utcnow()
    window_start = _floor_to_window(now) - timedelta(minutes=WINDOW_MINUTES)
    window_end = window_start + timedelta(minutes=WINDOW_MINUTES)

    # Idempotency: 同 window_start 不重发
    if not force:
        async with get_session() as session:
            existing = (await session.execute(
                select(CrashMetricAlert).where(CrashMetricAlert.window_start == window_start)
            )).scalars().first()
            if existing is not None:
                return {
                    "skipped": "already_alerted",
                    "window_start": window_start.isoformat(),
                }

    # 拉当前窗口
    try:
        current = await _fetch_crash_free(window_start, window_end)
    except Exception as exc:
        logger.exception("core_metric_alerter: datadog fetch failed")
        return {"ok": False, "error": f"datadog: {exc}",
                "window_start": window_start.isoformat()}

    if not current:
        return {"ok": True, "alerted": False, "reason": "no_data",
                "window_start": window_start.isoformat()}

    # 平台白名单
    platforms_filter = {
        p.strip().lower()
        for p in (getattr(s, "core_metric_platforms", "") or "").split(",")
        if p.strip()
    }

    threshold_pp = float(getattr(s, "core_metric_change_threshold_pp", 0.3) or 0.3)
    min_sessions = int(getattr(s, "core_metric_min_sessions", 100) or 0)

    alert_items: List[Dict[str, Any]] = []
    snapshot_log: List[Dict[str, Any]] = []

    async with get_session() as session:
        for platform, metrics in current.items():
            if platforms_filter and platform not in platforms_filter:
                continue
            total = int(metrics.get("total_sessions") or 0)
            crashed = int(metrics.get("crashed_sessions") or 0)
            cf_pct = float(metrics.get("crash_free_pct") or 0.0)

            # 写 snapshot（不论是否报警都入库——下次 tick 复用为基线）
            await _upsert_snapshot(session, window_start, platform, total, crashed, cf_pct)
            snapshot_log.append({"platform": platform, "total": total,
                                 "crashed": crashed, "cf_pct": cf_pct})

            # 量级闸：小流量不报警
            if total < min_sessions:
                continue

            baseline_cf = await _baseline_crash_free(session, platform, window_start)
            if baseline_cf is None:
                # 冷启动：前 1h 没积累够 snapshot，跳过本次判定
                continue
            delta_pp = cf_pct - baseline_cf
            if abs(delta_pp) < threshold_pp:
                continue
            direction = "down" if delta_pp < 0 else "up"
            alert_items.append({
                "platform": platform,
                "total_sessions": total,
                "crashed_sessions": crashed,
                "crash_free_pct": round(cf_pct, 3),
                "baseline_pct": round(baseline_cf, 3),
                "delta_pp": round(delta_pp, 3),
                "direction": direction,
            })
        await session.commit()

    if not alert_items:
        return {
            "ok": True, "alerted": False, "reason": "no_anomaly",
            "window_start": window_start.isoformat(),
            "snapshots": snapshot_log,
        }

    # 先记 alert 行拿 id（卡片 URL 深链需要）
    directions = {it["direction"] for it in alert_items}
    direction = (
        "mixed" if len(directions) > 1
        else next(iter(directions))
    )
    payload = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "threshold_pp": threshold_pp,
        "min_sessions": min_sessions,
        "items": alert_items,
        "all_snapshots": snapshot_log,
    }
    alert_id: Optional[int] = None
    try:
        async with get_session() as session:
            row = CrashMetricAlert(
                window_start=window_start,
                platforms_alerted=",".join(sorted(it["platform"] for it in alert_items)),
                direction=direction,
                feishu_message_id="",
                alert_payload=_json.dumps(payload, ensure_ascii=False, default=str),
            )
            session.add(row)
            await session.commit()
            alert_id = row.id
    except Exception:
        logger.exception("core_metric_alerter: alert insert race (ignored)")

    # 发卡片
    from app.crashguard.services.feishu_card import build_core_metric_alert_card
    card = build_core_metric_alert_card(
        window_start=window_start,
        items=alert_items,
        threshold_pp=threshold_pp,
        frontend_base_url=s.frontend_base_url,
        alert_id=alert_id,
    )
    sent_ok = False
    if not s.feishu_enabled:
        logger.info("core_metric_alerter: feishu_enabled=False, skip send (data 已落表)")
    else:
        # 路由：alert_email（点对点）> chat_id（群）> target_email（兼容）
        try:
            from app.services.feishu_cli import send_interactive_card
            if s.feishu_alert_email:
                sent_ok = await send_interactive_card(email=s.feishu_alert_email, card=card)
            elif s.feishu_target_chat_id:
                sent_ok = await send_interactive_card(chat_id=s.feishu_target_chat_id, card=card)
            elif s.feishu_target_email:
                sent_ok = await send_interactive_card(email=s.feishu_target_email, card=card)
            else:
                logger.warning("core_metric_alerter: no alert_email/chat_id; skip send")
        except Exception:
            logger.exception("core_metric_alerter: feishu send error")

    logger.info(
        "core_metric_alerter fired: window=%s alerts=%d direction=%s sent=%s",
        window_start.isoformat(), len(alert_items), direction, sent_ok,
    )
    return {
        "ok": True, "alerted": True, "sent": sent_ok,
        "direction": direction,
        "window_start": window_start.isoformat(),
        "items": alert_items,
        "alert_id": alert_id,
    }
