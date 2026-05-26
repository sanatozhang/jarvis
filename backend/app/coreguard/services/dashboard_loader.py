"""Dashboard 定义加载器 — 从 Datadog API 拉 widget JSON + metrics.yaml 校验。

底层逻辑：metrics.yaml 是「人维护的白名单」（含 tier / threshold），dashboard JSON 是
「Datadog 的真实 widget 定义」（含 queries / formulas）。两者按 widget_id 关联：
  - 加载时校验：widget_id 对应的 title 是否吻合（防 widget 顺序变动）
  - runtime 时：直接用 dashboard JSON 里的 queries + formula 调 v2/query/scalar
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml

from app.coreguard.config import get_coreguard_settings

logger = logging.getLogger("coreguard.dashboard_loader")


@dataclass
class MetricConfig:
    key: str
    title: str
    widget_id: int
    widget_type: str
    tier: str                      # P0 / P1 / P2
    value_type: str                # percent_pp / latency_pct / count_pct
    direction: str                 # down_is_bad / up_is_bad
    threshold: Dict[str, float]    # {"pp": 0.5} 或 {"pct": 0.20}
    alert_enabled: bool

    # 单指标 min_users override（None = 用 tier 级全局 settings.min_users）
    # 仅用于"小流量噪声大"的指标（音频导入 / AI 转写 / 云上传等）— v3 2026-05-26
    min_users: Optional[int] = None

    # 由 dashboard JSON 注入（启动时一次性）
    queries: Optional[List[Dict[str, Any]]] = None
    formula: Optional[str] = None
    # Datadog 真实 widget id（用于 fullscreen_widget 深链 — 让飞书卡片点开后直接定位 tile）
    datadog_widget_id: Optional[int] = None


@dataclass
class MetricsConfig:
    defaults: Dict[str, Any] = field(default_factory=dict)
    dashboard: Dict[str, Any] = field(default_factory=dict)
    metrics: List[MetricConfig] = field(default_factory=list)

    def by_key(self, key: str) -> Optional[MetricConfig]:
        for m in self.metrics:
            if m.key == key:
                return m
        return None

    def alertable(self) -> List[MetricConfig]:
        return [m for m in self.metrics if m.alert_enabled and m.queries is not None]


def _yaml_path() -> Path:
    return Path(__file__).resolve().parent.parent / "metrics.yaml"


def _load_yaml() -> dict:
    p = _yaml_path()
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


async def _fetch_dashboard(dashboard_id: str) -> Optional[dict]:
    s = get_coreguard_settings()
    if not s.datadog_api_key or not s.datadog_app_key:
        return None
    url = f"https://api.{s.datadog_site}/api/v1/dashboard/{dashboard_id}"
    headers = {
        "DD-API-KEY": s.datadog_api_key,
        "DD-APPLICATION-KEY": s.datadog_app_key,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning("dashboard fetch HTTP %s: %s", resp.status_code, resp.text[:300])
                return None
            return resp.json()
    except Exception as e:
        logger.warning("dashboard fetch failed: %s", e)
        return None


def _index_widgets(dashboard_json: dict) -> Dict[int, dict]:
    """按顺序 index → widget；保留 Datadog 真实 id 用于 fullscreen 深链。"""
    out: Dict[int, dict] = {}
    for idx, w in enumerate(dashboard_json.get("widgets", [])):
        d = dict(w.get("definition", {}))
        d["_dd_widget_id"] = w.get("id")
        out[idx] = d
    return out


async def load_metrics_config() -> MetricsConfig:
    """组装 MetricsConfig：yaml + dashboard JSON。"""
    raw = _load_yaml()
    cfg = MetricsConfig(
        defaults=raw.get("defaults", {}),
        dashboard=raw.get("dashboard", {}),
        metrics=[
            MetricConfig(
                key=m["key"],
                title=m["title"],
                widget_id=int(m["widget_id"]),
                widget_type=m.get("widget_type", "query_value"),
                tier=m.get("tier", "P2"),
                value_type=m.get("value_type", "percent_pp"),
                direction=m.get("direction", "down_is_bad"),
                threshold=m.get("threshold", {}),
                alert_enabled=bool(m.get("alert_enabled", False)),
                min_users=(int(m["min_users"]) if m.get("min_users") is not None else None),
            )
            for m in raw.get("metrics", [])
        ],
    )

    # 拉 dashboard JSON 注入 queries + formula
    dashboard_id = cfg.dashboard.get("id") or get_coreguard_settings().dashboard_id
    dj = await _fetch_dashboard(dashboard_id)
    if not dj:
        logger.warning("dashboard JSON not fetched, metrics will have no queries (datadog calls will fail)")
        return cfg

    widget_idx = _index_widgets(dj)

    mismatch = 0
    for m in cfg.metrics:
        defi = widget_idx.get(m.widget_id)
        if not defi:
            logger.warning("metric %s: widget_id %s out of range", m.key, m.widget_id)
            continue
        dd_title = (defi.get("title") or "").strip()
        if dd_title and dd_title != m.title:
            mismatch += 1
            logger.warning("metric %s: title mismatch (yaml=%r, dashboard=%r)", m.key, m.title, dd_title)
            # 不阻断，仍然加载 queries（title 漂移自查）
        m.datadog_widget_id = defi.get("_dd_widget_id")
        reqs = defi.get("requests", [])
        if not reqs:
            continue
        r0 = reqs[0]
        m.queries = r0.get("queries") or []
        formulas = r0.get("formulas") or []
        if formulas:
            m.formula = formulas[0].get("formula", "")

    logger.info("metrics loaded: total=%d alertable=%d mismatches=%d",
                len(cfg.metrics), len(cfg.alertable()), mismatch)
    return cfg


# 进程级缓存（启动后第一次调用 prime；reload 配置可手动清）
_cached: Optional[MetricsConfig] = None


async def get_metrics_config(force_reload: bool = False) -> MetricsConfig:
    global _cached
    if force_reload or _cached is None:
        _cached = await load_metrics_config()
    return _cached
