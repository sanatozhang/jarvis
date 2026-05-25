"""Coreguard demo 主链路。

闭环：
  1. fetch current window scalar  (Datadog v2 /query/scalar)
  2. fetch SHoW baseline scalar   (上周同小时)
  3. fetch sessions_count (low-base guard)
  4. 算 change_pp = current - baseline
  5. 入库 snapshot
  6. 超阈或 force → 发飞书卡片
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.coreguard.config import get_coreguard_settings
from app.coreguard.models import CoreguardMetricSnapshot
from app.coreguard.services import demo_metric as dm
from app.coreguard.services.datadog_scalar import query_scalar
from app.coreguard.services.feishu_card_demo import build_demo_alert_card
from app.db.database import get_session

logger = logging.getLogger("coreguard.demo_runner")


async def _send_feishu(card: Dict[str, Any]) -> bool:
    s = get_coreguard_settings()
    if not s.feishu_enabled:
        logger.info("feishu_enabled=false, skip send")
        return False
    if not s.feishu_target_chat_id and not s.feishu_target_email:
        logger.warning("no feishu target configured (chat_id / email)")
        return False
    try:
        from app.services.feishu_cli import send_interactive_card
        if s.feishu_target_chat_id:
            return await send_interactive_card(chat_id=s.feishu_target_chat_id, card=card)
        return await send_interactive_card(email=s.feishu_target_email, card=card)
    except Exception as e:
        logger.error("feishu send failed: %s", e)
        return False


async def run_demo(force_alert: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
    """跑一次 Crash-free sessions 监控全链路。

    Args:
        force_alert: 无视阈值/守门强制发卡片，用于看效果。
        now: 测试注入；None 取 UTC now。

    Returns:
        包含 current / baseline / change / breached / alert_sent / snapshot_id 的 dict。
    """
    s = get_coreguard_settings()
    now = now or datetime.utcnow()

    cur_start, cur_end = dm.current_window(now)
    base_start, base_end = dm.show_baseline_window(cur_start)

    logger.info(
        "demo_runner: current=[%s, %s) baseline=[%s, %s) force=%s",
        cur_start.isoformat(), cur_end.isoformat(),
        base_start.isoformat(), base_end.isoformat(), force_alert,
    )

    # 1. current value
    current_value = await query_scalar(
        queries=dm.CRASH_FREE_SESSIONS_QUERIES,
        formula=dm.CRASH_FREE_SESSIONS_FORMULA,
        start_ms=dm.to_ms(cur_start),
        end_ms=dm.to_ms(cur_end),
    )
    # 2. SHoW baseline
    baseline_value = await query_scalar(
        queries=dm.CRASH_FREE_SESSIONS_QUERIES,
        formula=dm.CRASH_FREE_SESSIONS_FORMULA,
        start_ms=dm.to_ms(base_start),
        end_ms=dm.to_ms(base_end),
    )
    # 3. sessions_count (current window) — 用于低基数守门 + 卡片展示
    sessions_value = await query_scalar(
        queries=dm.SESSIONS_ONLY_QUERIES,
        formula=dm.SESSIONS_ONLY_FORMULA,
        start_ms=dm.to_ms(cur_start),
        end_ms=dm.to_ms(cur_end),
    )
    sessions_count = int(sessions_value) if sessions_value is not None else None

    # 4. change
    change_pp: Optional[float] = None
    if current_value is not None and baseline_value is not None:
        change_pp = current_value - baseline_value

    # 5. breach 判定
    threshold_pp = s.demo_threshold_pp
    breached = False
    if change_pp is not None:
        # direction = down_is_bad → change_pp 越负越坏
        breached = change_pp <= -threshold_pp

    # 6. 入库
    snapshot_id: Optional[int] = None
    baseline_source = "show" if baseline_value is not None else "none"
    async with get_session() as session:
        # idempotent: 同 window_start 已存在则 update
        existing = (await session.execute(
            select(CoreguardMetricSnapshot).where(
                CoreguardMetricSnapshot.metric_key == dm.METRIC_KEY,
                CoreguardMetricSnapshot.window_start == cur_start,
            )
        )).scalar_one_or_none()
        if existing:
            existing.value = current_value
            existing.baseline_value = baseline_value
            existing.baseline_source = baseline_source
            existing.change = change_pp
            existing.sessions_count = sessions_count
            existing.breached = breached
            existing.value_type = dm.VALUE_TYPE
            existing.tier = "demo"
            existing.extra = json.dumps({"force_alert": force_alert}, ensure_ascii=False)
            snapshot_id = existing.id
        else:
            snap = CoreguardMetricSnapshot(
                metric_key=dm.METRIC_KEY,
                window_start=cur_start,
                value=current_value,
                baseline_value=baseline_value,
                baseline_source=baseline_source,
                change=change_pp,
                sessions_count=sessions_count,
                breached=breached,
                tier="demo",
                value_type=dm.VALUE_TYPE,
                alert_sent=False,
                extra=json.dumps({"force_alert": force_alert}, ensure_ascii=False),
            )
            session.add(snap)
            await session.flush()
            snapshot_id = snap.id
        await session.commit()

    # 7. 发飞书
    alert_sent = False
    should_alert = force_alert or breached
    if should_alert:
        # 修 naive datetime.timestamp() 时区 bug — 强制按 UTC 解释
        from datetime import timezone as _tz
        _from_ts = int(cur_start.replace(tzinfo=_tz.utc).timestamp() * 1000)
        _to_ts = int(cur_end.replace(tzinfo=_tz.utc).timestamp() * 1000)
        dashboard_url = (
            f"https://app.{s.datadog_site}/dashboard/{s.dashboard_id}"
            f"?from_ts={_from_ts}&to_ts={_to_ts}&live=false"
        )
        card = build_demo_alert_card(
            metric_title=dm.METRIC_TITLE,
            current_value=current_value if current_value is not None else float("nan"),
            baseline_value=baseline_value,
            change_pp=change_pp,
            threshold_pp=threshold_pp,
            sessions_count=sessions_count,
            current_window_label=f"{cur_start.strftime('%Y-%m-%d %H:%M')} ~ {cur_end.strftime('%H:%M')} UTC",
            baseline_window_label=f"{base_start.strftime('%Y-%m-%d %H:%M')} ~ {base_end.strftime('%H:%M')} UTC",
            dashboard_url=dashboard_url,
            forced=force_alert,
        )
        alert_sent = await _send_feishu(card)
        if alert_sent:
            async with get_session() as session:
                row = (await session.execute(
                    select(CoreguardMetricSnapshot).where(CoreguardMetricSnapshot.id == snapshot_id)
                )).scalar_one_or_none()
                if row:
                    row.alert_sent = True
                    await session.commit()

    return {
        "metric_key": dm.METRIC_KEY,
        "current_window": [cur_start.isoformat(), cur_end.isoformat()],
        "baseline_window": [base_start.isoformat(), base_end.isoformat()],
        "current_value": current_value,
        "baseline_value": baseline_value,
        "baseline_source": baseline_source,
        "change_pp": change_pp,
        "sessions_count": sessions_count,
        "threshold_pp": threshold_pp,
        "breached": breached,
        "force_alert": force_alert,
        "alert_sent": alert_sent,
        "snapshot_id": snapshot_id,
    }
