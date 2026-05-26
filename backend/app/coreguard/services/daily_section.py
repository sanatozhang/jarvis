"""为 crashguard 早报提供 coreguard 业务健康度板块的数据 + Markdown。

底层逻辑：
  - 早报由 crashguard 触发，但「核心指标」品牌要求合并展示稳定性 + 业务健康度
  - 这里输出 *数据 + 字符串*，不输出飞书卡 element；让 feishu_card 端拼装
  - 严格隔离：只读 coreguard_* 表，不 import crashguard

输入：target_date（昨日，UTC date）
输出：dict（详见 build_morning_section docstring）
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date as _date_t
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.coreguard.config import get_coreguard_settings
from app.coreguard.models import CoreguardMetricSnapshot
from app.db.database import get_session

logger = logging.getLogger("coreguard.daily_section")


# 默认 Datadog dashboard（深链兜底）— 真值见 config.dashboard_id
_DASHBOARD_BASE = "https://app.datadoghq.com/dashboard"


@dataclass
class _AggMetric:
    key: str
    title: str
    tier: str
    value_type: str
    direction: str
    threshold: Dict[str, float]
    datadog_widget_id: Optional[int] = None
    breach_windows: int = 0
    total_windows: int = 0
    longest_consecutive: int = 0  # 最长连续 breach 段（按 window_start 排序）
    worst_change: Optional[float] = None  # 单窗最严重的 change（按 abs 绝对值挑）
    worst_window_start: Optional[datetime] = None  # 最严重 breach 所在窗口
    worst_current_value: Optional[float] = None
    worst_baseline_value: Optional[float] = None


def _date_range_utc(target_date: _date_t) -> Tuple[datetime, datetime]:
    """target_date(=昨日, 一般是 today-1 BJT 日期) 在 UTC 上的 24h 窗口。

    crashguard daily_report 用 target_date 是 BJT 日历日（00:00-24:00 BJT）。
    我们要查 coreguard 快照对应的 UTC 窗口：BJT 当天 = UTC [前一日 16:00, 当日 16:00)。

    实际上为了简单 + 与 crashguard 对齐，这里直接用 UTC 当天 00:00-24:00。
    coreguard 快照的 window_start 也是 UTC（datetime.utcnow），统一好对齐。
    """
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def _emoji_tier(tier: str) -> str:
    return {"P0": "🚨", "P1": "⚠️", "P2": "📊"}.get(tier, "📊")


def _fmt_change(value_type: str, change: Optional[float]) -> str:
    if change is None:
        return "—"
    sign = "+" if change >= 0 else ""
    if value_type == "percent_pp":
        return f"{sign}{change:.2f} pp"
    return f"{sign}{change * 100:.1f}%"


def _fmt_value(value_type: str, v: Optional[float]) -> str:
    if v is None:
        return "—"
    if value_type == "percent_pp":
        return f"{v:.2f}%"
    return f"{v:.2f}"


def _dd_url(dashboard_id: str, datadog_site: str, widget_id: Optional[int],
            window_start: Optional[datetime], baseline_window_start: Optional[datetime] = None) -> str:
    """构造 Datadog widget fullscreen 深链。窗口 = 1 小时。"""
    base = f"https://app.{datadog_site}/dashboard/{dashboard_id}?live=false"
    if widget_id is not None:
        base += f"&fullscreen_widget={widget_id}"
    if window_start is not None:
        # 1h 窗口
        from_ts = int(window_start.replace(tzinfo=timezone.utc).timestamp() * 1000)
        to_ts = from_ts + 3600_000
        base += f"&from_ts={from_ts}&to_ts={to_ts}"
    return base


def _baseline_window_start(cur_start: datetime) -> datetime:
    """SHoW 基线 = cur_start - 7 天。"""
    return cur_start - timedelta(days=7)


async def _aggregate_snapshots(
    start_utc: datetime, end_utc: datetime
) -> Tuple[List[_AggMetric], int]:
    """按 metric_key 聚合快照。返回 (aggregated_list, total_windows_covered)."""
    async with get_session() as session:
        rows = (await session.execute(
            select(CoreguardMetricSnapshot)
            .where(
                CoreguardMetricSnapshot.window_start >= start_utc,
                CoreguardMetricSnapshot.window_start < end_utc,
            )
            .order_by(CoreguardMetricSnapshot.metric_key,
                      CoreguardMetricSnapshot.window_start)
        )).scalars().all()

    if not rows:
        return [], 0

    # 加载 yaml 元数据（拿 title / threshold / widget_id）
    try:
        from app.coreguard.services.dashboard_loader import get_metrics_config
        cfg = await get_metrics_config(force_reload=False)
        by_key = {m.key: m for m in cfg.metrics}
    except Exception as e:
        logger.warning("metrics_config load failed in daily_section: %s", e)
        by_key = {}

    # 按 metric_key group
    by_metric: Dict[str, List[CoreguardMetricSnapshot]] = defaultdict(list)
    for r in rows:
        by_metric[r.metric_key].append(r)

    out: List[_AggMetric] = []
    distinct_windows = set()
    for key, snaps in by_metric.items():
        meta = by_key.get(key)
        agg = _AggMetric(
            key=key,
            title=(meta.title if meta else key),
            tier=(meta.tier if meta else snaps[0].tier or "P2"),
            value_type=(meta.value_type if meta else snaps[0].value_type or "percent_pp"),
            direction=(meta.direction if meta else "down_is_bad"),
            threshold=(meta.threshold if meta else {}),
            datadog_widget_id=(meta.datadog_widget_id if meta else None),
            total_windows=len(snaps),
        )
        # 统计 breach + 最长连续 + 最严重
        consecutive = 0
        cur_run = 0
        for s in snaps:
            distinct_windows.add(s.window_start)
            if s.breached:
                agg.breach_windows += 1
                cur_run += 1
                if cur_run > consecutive:
                    consecutive = cur_run
                # 最严重：按 abs(change) 找
                ch = s.change
                if ch is not None:
                    if agg.worst_change is None or abs(ch) > abs(agg.worst_change):
                        agg.worst_change = ch
                        agg.worst_window_start = s.window_start
                        agg.worst_current_value = s.value
                        agg.worst_baseline_value = s.baseline_value
            else:
                cur_run = 0
        agg.longest_consecutive = consecutive
        out.append(agg)

    return out, len(distinct_windows)


def _classify(metrics: List[_AggMetric], persistent_threshold_hours: int = 2) -> Tuple[List[_AggMetric], List[_AggMetric], int]:
    """分桶：persistent(≥N 连续) / transient(单点) / healthy。"""
    persistent: List[_AggMetric] = []
    transient: List[_AggMetric] = []
    healthy_count = 0
    for m in metrics:
        if m.longest_consecutive >= persistent_threshold_hours:
            persistent.append(m)
        elif m.breach_windows > 0:
            transient.append(m)
        else:
            healthy_count += 1
    # 排序：P0 在前，按 abs(worst_change) desc
    def _key(m: _AggMetric):
        tier_order = {"P0": 0, "P1": 1, "P2": 2}.get(m.tier, 3)
        return (tier_order, -(abs(m.worst_change or 0)))
    persistent.sort(key=_key)
    transient.sort(key=_key)
    return persistent, transient, healthy_count


def _render_section_md(
    persistent: List[_AggMetric],
    transient: List[_AggMetric],
    healthy_count: int,
    total_metrics: int,
    windows_covered: int,
    dashboard_id: str,
    datadog_site: str,
) -> str:
    """生成业务健康度板块完整 lark_md 字符串。"""
    parts = [
        f"📊 评估 `{total_metrics * windows_covered}` 数据点（`{total_metrics}` 指标 × `{windows_covered}` 小时）"
    ]
    if persistent:
        lines = [f"🚨 **持续异常（≥2 小时连续 breach，已触发飞书实时告警）**："]
        for m in persistent:
            cur_url = _dd_url(dashboard_id, datadog_site, m.datadog_widget_id, m.worst_window_start)
            base_url = _dd_url(dashboard_id, datadog_site, m.datadog_widget_id,
                               _baseline_window_start(m.worst_window_start) if m.worst_window_start else None)
            cur_str = _fmt_value(m.value_type, m.worst_current_value)
            base_str = _fmt_value(m.value_type, m.worst_baseline_value)
            ch_str = _fmt_change(m.value_type, m.worst_change)
            lines.append(
                f"- {_emoji_tier(m.tier)} [{m.tier}] **{m.title}**: "
                f"最长连续 `{m.longest_consecutive}h` · 共 `{m.breach_windows}/{m.total_windows}` 次 breach\n"
                f"　• 最严重时段 Δ `{ch_str}`: 当前 [{cur_str}]({cur_url}) · 上周 [{base_str}]({base_url})"
            )
        parts.append("\n".join(lines))

    if transient:
        lines = [f"⚠️ **偶发异常（单点 breach，被 N=2 防抖拦下未发卡）**："]
        for m in transient[:5]:  # 最多展示 5 条
            cur_url = _dd_url(dashboard_id, datadog_site, m.datadog_widget_id, m.worst_window_start)
            base_url = _dd_url(dashboard_id, datadog_site, m.datadog_widget_id,
                               _baseline_window_start(m.worst_window_start) if m.worst_window_start else None)
            cur_str = _fmt_value(m.value_type, m.worst_current_value)
            base_str = _fmt_value(m.value_type, m.worst_baseline_value)
            ch_str = _fmt_change(m.value_type, m.worst_change)
            lines.append(
                f"- {_emoji_tier(m.tier)} [{m.tier}] **{m.title}**: "
                f"`{m.breach_windows}/{m.total_windows}` 次单点 · Δ `{ch_str}`\n"
                f"　• 当前 [{cur_str}]({cur_url}) · 上周 [{base_str}]({base_url})"
            )
        if len(transient) > 5:
            lines.append(f"- … 其余 `{len(transient) - 5}` 项偶发异常未展开")
        parts.append("\n".join(lines))

    parts.append(f"✅ `{healthy_count}/{total_metrics}` 指标全天在阈值内")
    parts.append(f"[打开 Datadog 全景 →]({_DASHBOARD_BASE}/{dashboard_id})")
    return "\n\n".join(parts)


def _build_headline_hint(persistent: List[_AggMetric], transient: List[_AggMetric]) -> Optional[str]:
    """给 crashguard headline 提供一句话提示（None = 业务侧没事，crashguard 用自己原 headline）。"""
    p0 = [m for m in persistent if m.tier == "P0"]
    p1 = [m for m in persistent if m.tier == "P1"]
    if p0:
        worst = p0[0]
        ch = _fmt_change(worst.value_type, worst.worst_change)
        return f"业务核心指标捕获 {len(p0)} 项 P0 持续异常：**{worst.title}** Δ `{ch}` ≥{worst.longest_consecutive}h"
    if p1:
        worst = p1[0]
        ch = _fmt_change(worst.value_type, worst.worst_change)
        return f"业务有 {len(p1)} 项 P1 持续异常：**{worst.title}** Δ `{ch}`"
    if transient:
        return f"业务侧 {len(transient)} 次偶发抖动（已被 N=2 防抖拦下，无需立即跟进）"
    return None


def _build_summary_chip(persistent: List[_AggMetric], transient: List[_AggMetric], total: int) -> str:
    """Σ 摘要行用的简短 chip。"""
    breach_total = len(persistent) + len(transient)
    parts = [f"业务异常 `{breach_total}/{total}`"]
    if persistent:
        parts.append(f"持续 `{len(persistent)}` ⚠️")
    return " · ".join(parts)


async def build_morning_section(
    target_date: _date_t,
    persistent_threshold_hours: int = 2,
) -> Dict[str, Any]:
    """供 crashguard daily_report 调用的统一入口。

    Returns:
      {
        "available": bool,             # False = 当天无 coreguard 数据 (e.g. cron 未跑)，crashguard 应跳过该板块
        "section_markdown": str,       # 整段 lark_md（折叠区里展示）
        "section_title_suffix": str,   # 折叠区标题后缀（"⚠️ (持续 N · 偶发 M)" 这种 chip）
        "auto_expand": bool,           # 有持续异常 → True（自动展开）
        "headline_hint": Optional[str], # 给 headline 拼装用的一句话
        "summary_chip": str,           # 给 Σ 摘要行用的 chip
        "persistent_count": int,
        "transient_count": int,
        "healthy_count": int,
        "total_metrics": int,
        "windows_covered": int,
      }
    """
    start_utc, end_utc = _date_range_utc(target_date)
    metrics, windows_covered = await _aggregate_snapshots(start_utc, end_utc)

    if not metrics or windows_covered == 0:
        return {
            "available": False,
            "section_markdown": "",
            "section_title_suffix": "",
            "auto_expand": False,
            "headline_hint": None,
            "summary_chip": "业务指标 `无数据`",
            "persistent_count": 0,
            "transient_count": 0,
            "healthy_count": 0,
            "total_metrics": 0,
            "windows_covered": 0,
        }

    persistent, transient, healthy_count = _classify(metrics, persistent_threshold_hours)
    total_metrics = len(metrics)
    settings = get_coreguard_settings()
    dashboard_id = settings.dashboard_id
    datadog_site = settings.datadog_site

    section_md = _render_section_md(
        persistent, transient, healthy_count, total_metrics, windows_covered,
        dashboard_id, datadog_site,
    )

    # 折叠区标题后缀
    if persistent:
        suffix = f"⚠️ (持续 {len(persistent)} · 偶发 {len(transient)})"
    elif transient:
        suffix = f"✅ (偶发 {len(transient)} 自恢复)"
    else:
        suffix = f"✅ ({healthy_count}/{total_metrics} 全天健康)"

    return {
        "available": True,
        "section_markdown": section_md,
        "section_title_suffix": suffix,
        "auto_expand": bool(persistent),  # 仅持续异常才自动展开
        "headline_hint": _build_headline_hint(persistent, transient),
        "summary_chip": _build_summary_chip(persistent, transient, total_metrics),
        "persistent_count": len(persistent),
        "transient_count": len(transient),
        "healthy_count": healthy_count,
        "total_metrics": total_metrics,
        "windows_covered": windows_covered,
    }
