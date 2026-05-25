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
from app.coreguard.services.dashboard_loader import get_metrics_config
from app.coreguard.services.demo_runner import run_demo
from app.coreguard.services.metric_watcher import run_all as watcher_run_all
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


@router.post("/run-all")
async def run_all_endpoint(
    dry_run: bool = Query(False, description="True=只入库不发飞书"),
    force_alert: bool = Query(False, description="True=即便全部正常也发一张演示卡"),
) -> Dict[str, Any]:
    """跑 metrics.yaml 中所有 alert_enabled 指标，一次性入库 + 聚合摘要发飞书。"""
    s = get_coreguard_settings()
    if not s.enabled:
        return {"ok": False, "reason": "coreguard disabled"}
    result = await watcher_run_all(dry_run=dry_run, force_alert=force_alert)
    return {"ok": True, **result}


@router.get("/metrics")
async def list_metrics() -> Dict[str, Any]:
    """列出 metrics.yaml 配置 + dashboard 注入的 queries 状态。"""
    cfg = await get_metrics_config(force_reload=False)
    items = []
    for m in cfg.metrics:
        items.append({
            "key": m.key, "title": m.title, "widget_id": m.widget_id,
            "tier": m.tier, "value_type": m.value_type, "direction": m.direction,
            "threshold": m.threshold, "alert_enabled": m.alert_enabled,
            "queries_loaded": m.queries is not None, "formula": m.formula,
        })
    return {
        "total": len(cfg.metrics),
        "alertable": len(cfg.alertable()),
        "dashboard_id": cfg.dashboard.get("id"),
        "items": items,
    }


@router.post("/reload-config")
async def reload_config() -> Dict[str, Any]:
    """强制重载 metrics.yaml + 重拉 dashboard JSON。"""
    cfg = await get_metrics_config(force_reload=True)
    return {"ok": True, "total": len(cfg.metrics), "alertable": len(cfg.alertable())}


@router.post("/jobs/trigger")
async def trigger_job(job: str = Query("coreguard_hourly_watch")) -> Dict[str, Any]:
    """手动触发一次调度 job — 等价 cron 跑一次（写 heartbeat + 真发 email）。"""
    if job == "coreguard_hourly_watch":
        from app.coreguard.workers.scheduler import _run_hourly_watch_once
        await _run_hourly_watch_once()
        return {"ok": True, "job": job}
    return {"ok": False, "reason": f"unknown job: {job}"}


@router.get("/jobs/status")
async def jobs_status(limit: int = Query(10, ge=1, le=50)) -> Dict[str, Any]:
    """查 scheduler 心跳 — 最近 N 次每个 job 的执行状态。"""
    from app.coreguard.models import CoreguardJobHeartbeat
    out: Dict[str, Any] = {"jobs": {}}
    async with get_session() as session:
        rows = (await session.execute(
            select(CoreguardJobHeartbeat).order_by(CoreguardJobHeartbeat.fired_at.desc()).limit(limit * 5)
        )).scalars().all()
    by_job: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_job.setdefault(r.job_name, []).append({
            "fired_at": r.fired_at.isoformat() if r.fired_at else None,
            "status": r.status,
            "duration_ms": r.duration_ms,
            "summary": r.summary,
            "error": r.error or None,
        })
    for job, items in by_job.items():
        out["jobs"][job] = items[:limit]
    return out


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
