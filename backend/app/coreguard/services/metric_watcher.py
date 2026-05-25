"""Metric watcher — 循环跑所有 alert_enabled 指标的 SHoW 对比 + 入库 + 聚合发飞书。

闭环：
  for cfg in metrics.yaml (alert_enabled=true):
    cur_val  = scalar(cfg.queries, cfg.formula, [now-1h, now))
    base_val = scalar(cfg.queries, cfg.formula, [now-1h-7d, now-7d))
    breached = threshold_judge(cfg, cur, base)
    snapshot.upsert(cfg.key, ...)
  emit_summary_card(all_results, dry_run=...)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.coreguard.config import get_coreguard_settings
from app.coreguard.models import CoreguardMetricSnapshot
from app.coreguard.services import demo_metric as dm
from app.coreguard.services.dashboard_loader import (
    MetricConfig,
    MetricsConfig,
    get_metrics_config,
)
from app.coreguard.services.datadog_scalar import query_scalar
from app.db.database import get_session

logger = logging.getLogger("coreguard.metric_watcher")


@dataclass
class MetricResult:
    key: str
    title: str
    tier: str
    value_type: str
    direction: str
    threshold: Dict[str, float]
    current_value: Optional[float]
    baseline_value: Optional[float]
    change: Optional[float]              # pp 或 pct（按 value_type）
    sessions_count: Optional[int]
    breached: bool
    error: Optional[str] = None


def _judge(cfg: MetricConfig, cur: Optional[float], base: Optional[float]) -> tuple[bool, Optional[float]]:
    """单点判定：breached + change（按 value_type）。"""
    if cur is None or base is None:
        return False, None
    if cfg.value_type == "percent_pp":
        change = cur - base
        thresh = float(cfg.threshold.get("pp", 1.0))
        if cfg.direction == "down_is_bad":
            return change <= -thresh, change
        else:  # up_is_bad
            return change >= thresh, change
    else:  # latency_pct / count_pct
        if base <= 0:
            return False, None
        change = (cur - base) / base
        thresh = float(cfg.threshold.get("pct", 0.20))
        if cfg.direction == "down_is_bad":
            return change <= -thresh, change
        else:
            return change >= thresh, change


async def _scalar_safe(queries, formula, s_ms, e_ms) -> Optional[float]:
    try:
        return await query_scalar(queries=queries, formula=formula, start_ms=s_ms, end_ms=e_ms)
    except Exception as e:
        logger.warning("scalar call failed: %s", e)
        return None


async def evaluate_one(cfg: MetricConfig, cur_start, cur_end, base_start, base_end) -> MetricResult:
    """单指标评估：拉 current + baseline → judge."""
    if not cfg.queries or not cfg.formula:
        return MetricResult(
            key=cfg.key, title=cfg.title, tier=cfg.tier, value_type=cfg.value_type,
            direction=cfg.direction, threshold=cfg.threshold,
            current_value=None, baseline_value=None, change=None, sessions_count=None,
            breached=False, error="missing queries/formula",
        )
    # 并发拉 current + baseline 节省时间
    cur_val, base_val = await asyncio.gather(
        _scalar_safe(cfg.queries, cfg.formula, dm.to_ms(cur_start), dm.to_ms(cur_end)),
        _scalar_safe(cfg.queries, cfg.formula, dm.to_ms(base_start), dm.to_ms(base_end)),
    )
    breached, change = _judge(cfg, cur_val, base_val)
    return MetricResult(
        key=cfg.key, title=cfg.title, tier=cfg.tier, value_type=cfg.value_type,
        direction=cfg.direction, threshold=cfg.threshold,
        current_value=cur_val, baseline_value=base_val, change=change,
        sessions_count=None,  # 全量循环跑暂不拉 sessions（量太大），后续可按需补
        breached=breached, error=None,
    )


async def _persist_snapshot(r: MetricResult, cur_start) -> None:
    async with get_session() as session:
        existing = (await session.execute(
            select(CoreguardMetricSnapshot).where(
                CoreguardMetricSnapshot.metric_key == r.key,
                CoreguardMetricSnapshot.window_start == cur_start,
            )
        )).scalar_one_or_none()
        baseline_source = "show" if r.baseline_value is not None else ("error" if r.error else "none")
        if existing:
            existing.value = r.current_value
            existing.baseline_value = r.baseline_value
            existing.baseline_source = baseline_source
            existing.change = r.change
            existing.sessions_count = r.sessions_count
            existing.breached = r.breached
            existing.tier = r.tier
            existing.value_type = r.value_type
            existing.extra = json.dumps({"direction": r.direction, "error": r.error}, ensure_ascii=False)
        else:
            session.add(CoreguardMetricSnapshot(
                metric_key=r.key,
                window_start=cur_start,
                value=r.current_value,
                baseline_value=r.baseline_value,
                baseline_source=baseline_source,
                change=r.change,
                sessions_count=r.sessions_count,
                breached=r.breached,
                tier=r.tier,
                value_type=r.value_type,
                alert_sent=False,
                extra=json.dumps({"direction": r.direction, "error": r.error}, ensure_ascii=False),
            ))
        await session.commit()


async def run_all(dry_run: bool = False, now: Optional[datetime] = None,
                  force_alert: bool = False) -> Dict[str, Any]:
    """跑所有 alert_enabled 指标。

    Args:
        dry_run: True → 入库但不发飞书。
        force_alert: True → 即便没有指标超阈也发一张"全绿"卡片，看效果。
    """
    now = now or datetime.utcnow()
    cur_start, cur_end = dm.current_window(now)
    base_start, base_end = dm.show_baseline_window(cur_start)

    cfg = await get_metrics_config(force_reload=False)
    targets = cfg.alertable()
    logger.info("metric_watcher.run_all: total=%d targets=%d dry_run=%s force=%s",
                len(cfg.metrics), len(targets), dry_run, force_alert)

    # 串行跑（避免一次性并发 22 个 datadog 请求被限流；可后续调成 gather 分批）
    results: List[MetricResult] = []
    for c in targets:
        r = await evaluate_one(c, cur_start, cur_end, base_start, base_end)
        await _persist_snapshot(r, cur_start)
        results.append(r)

    breached = [r for r in results if r.breached]
    healthy = [r for r in results if not r.breached and r.error is None and r.current_value is not None]
    errored = [r for r in results if r.error is not None or r.current_value is None]

    alert_sent = False
    if not dry_run and (breached or force_alert):
        from app.coreguard.services.feishu_summary_card import build_summary_card, send
        card = build_summary_card(
            cur_start=cur_start, cur_end=cur_end,
            base_start=base_start, base_end=base_end,
            breached=results_to_dict(breached),
            healthy=results_to_dict(healthy),
            errored=results_to_dict(errored),
            forced=force_alert,
            dashboard_id=cfg.dashboard.get("id") or get_coreguard_settings().dashboard_id,
            datadog_site=get_coreguard_settings().datadog_site,
        )
        alert_sent = await send(card)

    return {
        "current_window": [cur_start.isoformat(), cur_end.isoformat()],
        "baseline_window": [base_start.isoformat(), base_end.isoformat()],
        "evaluated": len(results),
        "breached": len(breached),
        "healthy": len(healthy),
        "errored": len(errored),
        "dry_run": dry_run,
        "force_alert": force_alert,
        "alert_sent": alert_sent,
        "results": results_to_dict(results),
    }


def results_to_dict(rs: List[MetricResult]) -> List[Dict[str, Any]]:
    return [asdict(r) for r in rs]
