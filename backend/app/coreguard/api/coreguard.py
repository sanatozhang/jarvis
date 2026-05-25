"""Coreguard API router（demo 阶段最小集）。

prefix: /api/coreguard
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.coreguard.config import get_coreguard_settings
from app.coreguard.models import CoreguardMetricSnapshot
from app.coreguard.services.demo_runner import run_demo
from app.db.database import get_session

logger = logging.getLogger("coreguard.api")

router = APIRouter(prefix="/api/coreguard", tags=["Coreguard"])


@router.get("/health")
async def health() -> Dict[str, Any]:
    s = get_coreguard_settings()
    return {
        "ok": True,
        "enabled": s.enabled,
        "feishu_enabled": s.feishu_enabled,
        "datadog_configured": bool(s.datadog_api_key and s.datadog_app_key),
        "feishu_target_configured": bool(s.feishu_target_chat_id or s.feishu_target_email),
        "dashboard_id": s.dashboard_id,
    }


@router.post("/demo-run")
async def demo_run(
    force_alert: bool = Query(False, description="无视阈值强制发飞书卡片（看效果用）"),
) -> Dict[str, Any]:
    """跑一次 Crash-free sessions demo 全链路。

    手动触发，无 cron 调度。返回 current/baseline/change/alert_sent 等。
    """
    s = get_coreguard_settings()
    if not s.enabled:
        return {"ok": False, "reason": "coreguard disabled"}
    result = await run_demo(force_alert=force_alert)
    return {"ok": True, **result}


@router.get("/snapshots")
async def list_snapshots(
    metric_key: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """看 snapshot 历史（demo 验证用）。"""
    async with get_session() as session:
        stmt = select(CoreguardMetricSnapshot).order_by(
            CoreguardMetricSnapshot.window_start.desc(),
            CoreguardMetricSnapshot.id.desc(),
        ).limit(limit)
        if metric_key:
            stmt = stmt.where(CoreguardMetricSnapshot.metric_key == metric_key)
        rows = (await session.execute(stmt)).scalars().all()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r.id,
            "metric_key": r.metric_key,
            "window_start": r.window_start.isoformat() if r.window_start else None,
            "value": r.value,
            "baseline_value": r.baseline_value,
            "baseline_source": r.baseline_source,
            "change": r.change,
            "sessions_count": r.sessions_count,
            "breached": r.breached,
            "alert_sent": r.alert_sent,
            "tier": r.tier,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"items": out, "count": len(out)}
