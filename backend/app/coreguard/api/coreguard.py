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


# ---------------------------------------------------------------------------
# Jobs status — crashguard 兼容 shape，让前端 /crashguard/jobs 页能聚合显示
# ---------------------------------------------------------------------------
# 隔离合约：coreguard 拷贝 cron 解析助手而不是 import crashguard，保持模块独立。
_COREGUARD_JOB_META: List[Dict[str, str]] = [
    {
        "name": "coreguard_hourly_watch",
        "cron_field": "hourly_watch_cron",
        "label": "Coreguard 小时监控",
        "desc": "22 个核心指标 SHoW 对比，breach 走飞书/邮件告警",
        "enabled_field": "scheduler_enabled",
    },
]


def _coreguard_next_fire_time(cron_expr: str, now_dt) -> Optional[str]:
    """对极简 cron 算下一个触发时刻。仅支持 `M H * * *` 或 `*/N`。"""
    from datetime import datetime, timedelta
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    if dom_f != "*" or month_f != "*" or dow_f != "*":
        return None

    def _step(f: str) -> Optional[int]:
        if f.startswith("*/"):
            try:
                return int(f[2:])
            except ValueError:
                return None
        return None

    def _fixed(f: str) -> Optional[int]:
        if f == "*":
            return None
        try:
            return int(f)
        except ValueError:
            return None

    cur = (now_dt or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 + 60):
        m_ok = (minute_f == "*"
                or (_step(minute_f) is not None and cur.minute % _step(minute_f) == 0)
                or (_fixed(minute_f) is not None and cur.minute == _fixed(minute_f)))
        h_ok = (hour_f == "*"
                or (_step(hour_f) is not None and cur.hour % _step(hour_f) == 0)
                or (_fixed(hour_f) is not None and cur.hour == _fixed(hour_f)))
        if m_ok and h_ok:
            return cur.isoformat()
        cur += timedelta(minutes=1)
    return None


def _coreguard_interval_minutes_from_cron(cron_expr: str) -> Optional[int]:
    """从极简 cron 推算"两次触发预期间隔分钟数"，用于 stale 判定。"""
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, *_ = parts
    if minute_f.startswith("*/"):
        try:
            return max(1, int(minute_f[2:]))
        except ValueError:
            return None
    if hour_f.startswith("*/"):
        try:
            return max(1, int(hour_f[2:]) * 60)
        except ValueError:
            return None
    # M H * * *：固定分钟 + 固定小时 → 一天一次（1440min）
    if minute_f != "*" and hour_f != "*":
        return 24 * 60
    # M * * * *：每小时固定第 M 分钟 → 60min
    if minute_f != "*" and hour_f == "*":
        return 60
    return None


@router.get("/jobs/status")
async def jobs_status() -> Dict[str, Any]:
    """所有 coreguard 定时任务的 cron + 上次心跳 + 健康度判定。

    Shape 与 `/api/crash/jobs/status` 一致（items 列表，每项含 cron / health 等），
    便于前端 `/crashguard/jobs` 页统一聚合渲染。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import desc
    from app.coreguard.models import CoreguardJobHeartbeat
    import json as _json

    s = get_coreguard_settings()
    now = datetime.now()
    now_utc = datetime.utcnow()

    items: List[Dict[str, Any]] = []
    async with get_session() as session:
        for meta in _COREGUARD_JOB_META:
            jn = meta["name"]
            cron_expr = getattr(s, meta["cron_field"], "") if meta["cron_field"] else ""
            enabled_flag = (
                bool(getattr(s, meta["enabled_field"], True))
                if meta["enabled_field"] else True
            )

            last_row = (await session.execute(
                select(CoreguardJobHeartbeat)
                .where(CoreguardJobHeartbeat.job_name == jn)
                .order_by(desc(CoreguardJobHeartbeat.fired_at))
                .limit(1)
            )).scalars().first()
            # 兼容历史心跳：早期 coreguard 用 "ok"，对齐后用 "success"。
            # 两个都识别为成功，避免老数据被误判 stale。
            last_success_row = (await session.execute(
                select(CoreguardJobHeartbeat)
                .where(
                    CoreguardJobHeartbeat.job_name == jn,
                    CoreguardJobHeartbeat.status.in_(["success", "ok"]),
                )
                .order_by(desc(CoreguardJobHeartbeat.fired_at))
                .limit(1)
            )).scalars().first()
            recent = (await session.execute(
                select(CoreguardJobHeartbeat)
                .where(CoreguardJobHeartbeat.job_name == jn)
                .order_by(desc(CoreguardJobHeartbeat.fired_at))
                .limit(50)
            )).scalars().all()
            fail_count_50 = sum(1 for r in recent if r.status == "failed")
            # 历史心跳 "partial" 等价 crashguard "degraded"，对齐后都用 "degraded"
            degraded_count_50 = sum(1 for r in recent if r.status in ("degraded", "partial"))
            consecutive_failures = 0
            for r in recent:
                if r.status == "failed":
                    consecutive_failures += 1
                else:
                    break
            consecutive_unhealthy = 0
            for r in recent:
                if r.status in ("degraded", "partial", "failed"):
                    consecutive_unhealthy += 1
                else:
                    break

            interval_minutes = _coreguard_interval_minutes_from_cron(cron_expr)
            stale = False
            if interval_minutes and last_success_row is not None and last_success_row.fired_at:
                age_minutes = (now_utc - last_success_row.fired_at).total_seconds() / 60.0
                if age_minutes > 2 * interval_minutes:
                    stale = True
            elif interval_minutes and last_success_row is None and last_row is not None:
                stale = True

            last_summary: Dict[str, Any] = {}
            if last_row and last_row.summary:
                try:
                    last_summary = _json.loads(last_row.summary or "{}")
                except Exception:
                    last_summary = {}

            items.append({
                "name": jn,
                "label": meta["label"],
                "desc": meta["desc"],
                "cron": cron_expr,
                "enabled": enabled_flag,
                "interval_minutes": interval_minutes,
                "next_fire_at": _coreguard_next_fire_time(cron_expr, now) if cron_expr else None,
                "last_fired_at": last_row.fired_at.isoformat() if last_row and last_row.fired_at else None,
                "last_status": last_row.status if last_row else None,
                "last_duration_ms": int(last_row.duration_ms or 0) if last_row else 0,
                "last_error": (last_row.error or "")[:300] if last_row else "",
                "last_summary": last_summary,
                "last_success_at": (
                    last_success_row.fired_at.isoformat()
                    if last_success_row and last_success_row.fired_at else None
                ),
                "fail_count_in_recent_50": fail_count_50,
                "degraded_count_in_recent_50": degraded_count_50,
                "consecutive_failures": consecutive_failures,
                "consecutive_unhealthy": consecutive_unhealthy,
                "stale": stale,
                "health": (
                    "stale" if stale
                    else "failing" if consecutive_failures >= 3
                    else "failing" if consecutive_unhealthy >= 6
                    else "degraded" if (fail_count_50 + degraded_count_50) >= 10
                    else "ok"
                ),
                # 前端按这个字段分发 trigger / heartbeats 调用到正确模块
                "module": "coreguard",
            })

    return {
        "items": items,
        "server_time_local": now.isoformat(),
        "server_time_utc": now_utc.isoformat(),
    }


@router.get("/jobs/{job_name}/heartbeats")
async def job_heartbeats(job_name: str, limit: int = Query(50, ge=1, le=200)) -> Dict[str, Any]:
    """单个 job 的最近 N 次心跳历史。"""
    from sqlalchemy import desc
    from app.coreguard.models import CoreguardJobHeartbeat

    items: List[Dict[str, Any]] = []
    async with get_session() as session:
        rows = (await session.execute(
            select(CoreguardJobHeartbeat)
            .where(CoreguardJobHeartbeat.job_name == job_name)
            .order_by(desc(CoreguardJobHeartbeat.fired_at))
            .limit(limit)
        )).scalars().all()
        for r in rows:
            items.append({
                "fired_at": r.fired_at.isoformat() if r.fired_at else None,
                "status": r.status,
                "duration_ms": int(r.duration_ms or 0),
                "summary": r.summary or "",
                "error": (r.error or "")[:500],
            })
    return {"job_name": job_name, "items": items}


@router.post("/jobs/{job_name}/run-now")
async def run_job_now(job_name: str) -> Dict[str, Any]:
    """手动触发一次 job — 写心跳 + 真实业务效果（同 cron 路径）。"""
    if job_name == "coreguard_hourly_watch":
        from app.coreguard.workers.scheduler import _run_hourly_watch_once
        await _run_hourly_watch_once()
        return {"ok": True, "job": job_name}
    return {"ok": False, "reason": f"unknown coreguard job: {job_name}"}


# 保留旧 trigger endpoint 一段时间做向后兼容（无外部消费者，下个 sprint 可删）
@router.post("/jobs/trigger")
async def trigger_job(job: str = Query("coreguard_hourly_watch")) -> Dict[str, Any]:
    """[Deprecated] 改用 POST /jobs/{job_name}/run-now。"""
    return await run_job_now(job)


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
