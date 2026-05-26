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
    breached: bool                       # 原始判定（仅看本时段阈值，进 DB 快照）
    alertable: bool = False              # 通过 min_users / N=2 防抖 后才入飞书卡
    skip_reason: Optional[str] = None    # 被 gate 拦下时记原因（如 min_users 兜底 / 防抖等下次）
    datadog_widget_id: Optional[int] = None  # Datadog widget 真实 id（fullscreen 深链用）
    error: Optional[str] = None


def _judge(cfg: MetricConfig, cur: Optional[float], base: Optional[float]) -> tuple[bool, Optional[float]]:
    """单点判定：breached + change（按 value_type）。

    支持 4 种 value_type：
      - percent_pp        : SHoW 绝对百分点差 cur-base 与 threshold.pp 比
      - latency_pct       : SHoW 相对比例 (cur-base)/base 与 threshold.pct 比
      - count_pct         : 同 latency_pct
      - absolute_threshold: 不走 SHoW，cur 直接与 threshold.red 比（用于
                            dashboard 已定义业务红线的指标如 Hang Rate / ANR）
    """
    if cur is None:
        return False, None

    if cfg.value_type == "absolute_threshold":
        red = cfg.threshold.get("red")
        if red is None:
            return False, None
        red = float(red)
        change = cur - red  # change = 超出红线多少（正=已越红线）
        if cfg.direction == "up_is_bad":
            return cur >= red, change
        if cfg.direction == "down_is_bad":
            return cur <= red, change
        return abs(change) >= 0, change

    # 以下走 SHoW 对比，需要 base
    if base is None:
        return False, None
    if cfg.value_type == "percent_pp":
        change = cur - base
        thresh = float(cfg.threshold.get("pp", 1.0))
    else:  # latency_pct / count_pct
        if base <= 0:
            return False, None
        change = (cur - base) / base
        thresh = float(cfg.threshold.get("pct", 0.20))
    if cfg.direction == "down_is_bad":
        return change <= -thresh, change
    if cfg.direction == "up_is_bad":
        return change >= thresh, change
    return abs(change) >= thresh, change


async def _scalar_safe(queries, formula, s_ms, e_ms) -> Optional[float]:
    try:
        return await query_scalar(queries=queries, formula=formula, start_ms=s_ms, end_ms=e_ms)
    except Exception as e:
        logger.warning("scalar call failed: %s", e)
        return None


_GLOBAL_SESSION_FILTER = "@type:session @session.type:user"


async def _query_distinct_users(search_filter: str, s_ms: int, e_ms: int) -> int:
    """通用：用 search filter 跑 cardinality(@usr.id)。失败回 0（保守 → 触发 gate）。"""
    try:
        v = await query_scalar(
            queries=[{
                "name": "u",
                "data_source": "rum",
                "search": {"query": search_filter},
                "indexes": ["*"],
                "compute": {"aggregation": "cardinality", "metric": "@usr.id"},
                "group_by": [],
            }],
            formula="u",
            start_ms=s_ms, end_ms=e_ms,
        )
        return int(v or 0)
    except Exception as e:
        logger.warning("user_count scalar failed for filter=%r: %s", search_filter, e)
        return 0


async def _user_count_safe(s_ms: int, e_ms: int) -> int:
    """[兼容] 全局 session 用户数。新代码请用 _metric_user_count。"""
    return await _query_distinct_users(_GLOBAL_SESSION_FILTER, s_ms, e_ms)


def _strip_template_vars(q: str) -> str:
    """剔除 dashboard template vars（$os_name / $version），scalar API 不识别。"""
    if not q:
        return ""
    return q.replace("$os_name", "").replace("$version", "").strip()


async def _metric_user_count(cfg: MetricConfig, s_ms: int, e_ms: int,
                              cache: Dict[str, int]) -> Optional[int]:
    """按 metric 自身 queries 的 search filter 求 distinct user_count，取 MAX（代表真实人群）。

    Args:
        cfg: 单个 MetricConfig
        s_ms, e_ms: 窗口时间戳（ms）
        cache: 同窗口内的 filter→count 缓存（多个 metric 共享相同 filter 时复用）

    Returns:
        - RUM 类型：MAX(各 query 的 cardinality(@usr.id))
        - metrics 类型（ANR/Hang/Memory/Refresh）：None，调用方应回落到全局 user_count
    """
    if not cfg.queries:
        return None
    ds = (cfg.queries[0].get("data_source") or "metrics").lower()
    if ds != "rum":
        return None  # metrics 类型无法 cardinality，调用方决策回落

    counts: List[int] = []
    for q in cfg.queries:
        f = _strip_template_vars((q.get("search") or {}).get("query") or "")
        if not f:
            continue
        if f in cache:
            counts.append(cache[f])
            continue
        c = await _query_distinct_users(f, s_ms, e_ms)
        cache[f] = c
        counts.append(c)
    return max(counts) if counts else None


async def _was_breached_in_prev_window(metric_key: str, cur_start: datetime) -> bool:
    """N=2 防抖辅助：查询同 metric_key 在「当前 cur_start 之前一个窗口」是否 breached。

    依赖小时颗粒度对齐：上一个 cur_start = cur_start - 1h。
    若没有记录，视为 not breached（首次出现的 breach 不直接报）。
    """
    from datetime import timedelta
    prev_start = cur_start - timedelta(hours=1)
    async with get_session() as session:
        row = (await session.execute(
            select(CoreguardMetricSnapshot.breached).where(
                CoreguardMetricSnapshot.metric_key == metric_key,
                CoreguardMetricSnapshot.window_start == prev_start,
            )
        )).scalar_one_or_none()
        return bool(row)


async def evaluate_one(cfg: MetricConfig, cur_start, cur_end, base_start, base_end) -> MetricResult:
    """单指标评估：拉 current + baseline → judge."""
    if not cfg.queries or not cfg.formula:
        return MetricResult(
            key=cfg.key, title=cfg.title, tier=cfg.tier, value_type=cfg.value_type,
            direction=cfg.direction, threshold=cfg.threshold,
            current_value=None, baseline_value=None, change=None, sessions_count=None,
            breached=False,
            datadog_widget_id=cfg.datadog_widget_id,
            error="missing queries/formula",
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
        breached=breached,
        datadog_widget_id=cfg.datadog_widget_id,
        error=None,
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
            existing.extra = json.dumps({
                "direction": r.direction, "error": r.error,
                "alertable": r.alertable, "skip_reason": r.skip_reason,
            }, ensure_ascii=False)
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
                extra=json.dumps({
                    "direction": r.direction, "error": r.error,
                    "alertable": r.alertable, "skip_reason": r.skip_reason,
                }, ensure_ascii=False),
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
    settings = get_coreguard_settings()
    min_users = int(getattr(settings, "min_users", 300) or 0)
    p1_n = int(getattr(settings, "p1_consecutive_breach", 2) or 1)
    logger.info("metric_watcher.run_all: total=%d targets=%d dry_run=%s force=%s",
                len(cfg.metrics), len(targets), dry_run, force_alert)

    s_ms, e_ms = dm.to_ms(cur_start), dm.to_ms(cur_end)
    # 全局 session 用户数：metrics-type 指标（ANR/Hang/Memory/Refresh）回落用
    global_user_count = await _query_distinct_users(_GLOBAL_SESSION_FILTER, s_ms, e_ms)
    logger.info("metric_watcher: global session user_count=%d", global_user_count)

    # per-metric user_count 缓存（同窗口内同 filter 复用，避免 N+1）
    user_count_cache: Dict[str, int] = {_GLOBAL_SESSION_FILTER: global_user_count}

    # 串行跑（避免一次性并发 22 个 datadog 请求被限流；可后续调成 gather 分批）
    results: List[MetricResult] = []
    for c in targets:
        r = await evaluate_one(c, cur_start, cur_end, base_start, base_end)
        # per-metric user_count（rum）or 回落到全局（metrics 类型）
        muc = await _metric_user_count(c, s_ms, e_ms, user_count_cache)
        if muc is None:
            muc = global_user_count  # metrics 类型回落
        r.sessions_count = muc       # 颗粒度对齐：这个 metric 真实人群
        # 每指标 effective_min_users = override > global
        effective_min = int(c.min_users) if c.min_users is not None else min_users
        # Gate A: 样本量地板（per-metric）
        if r.breached and muc < effective_min:
            r.alertable = False
            r.skip_reason = f"min_users 兜底 ({muc} < {effective_min}, metric 口径)"
        # Gate B: P1 N=2 防抖（P0 不走防抖立即报）
        elif r.breached and r.tier == "P1" and p1_n >= 2:
            prev_breached = await _was_breached_in_prev_window(r.key, cur_start)
            if not prev_breached:
                r.alertable = False
                r.skip_reason = f"N={p1_n} 防抖：上窗口未 breach，本次仅记录"
            else:
                r.alertable = True
        else:
            r.alertable = bool(r.breached)
        await _persist_snapshot(r, cur_start)
        results.append(r)

    breached_raw = [r for r in results if r.breached]
    alertable = [r for r in results if r.alertable]
    suppressed = [r for r in results if r.breached and not r.alertable]
    healthy = [r for r in results if not r.breached and r.error is None and r.current_value is not None]
    errored = [r for r in results if r.error is not None or r.current_value is None]

    alert_sent = False
    if not dry_run and (alertable or force_alert):
        from app.coreguard.services.feishu_summary_card import build_summary_card, send
        card = build_summary_card(
            cur_start=cur_start, cur_end=cur_end,
            base_start=base_start, base_end=base_end,
            breached=results_to_dict(alertable),   # 只把通过 gate 的入卡片
            healthy=results_to_dict(healthy),
            errored=results_to_dict(errored),
            forced=force_alert,
            dashboard_id=cfg.dashboard.get("id") or settings.dashboard_id,
            datadog_site=settings.datadog_site,
        )
        alert_sent = await send(card)

    return {
        "current_window": [cur_start.isoformat(), cur_end.isoformat()],
        "baseline_window": [base_start.isoformat(), base_end.isoformat()],
        "evaluated": len(results),
        "breached_raw": len(breached_raw),
        "alertable": len(alertable),
        "suppressed": len(suppressed),
        "healthy": len(healthy),
        "errored": len(errored),
        "user_count": user_count,
        "min_users": min_users,
        "dry_run": dry_run,
        "force_alert": force_alert,
        "alert_sent": alert_sent,
        "results": results_to_dict(results),
    }


def results_to_dict(rs: List[MetricResult]) -> List[Dict[str, Any]]:
    return [asdict(r) for r in rs]
