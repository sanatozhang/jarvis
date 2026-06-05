"""coreguard 指标方向语义回归测试。

背景（2026-06-05 事故）：wifi同步文件速度P75 是吞吐量指标（KB/s，越高越好，
Datadog widget 自身 directionality=increase_better），却被配成 direction=up_is_bad，
导致速度上涨 +37.71% 被误报 P1 异常。本测试锁死方向，防回归。
"""
from __future__ import annotations

import yaml
import pytest

from app.coreguard.services.metric_watcher import _judge, MetricConfig


def _load_metrics():
    import app.coreguard.services.dashboard_loader as dl
    # metrics.yaml 与 dashboard_loader 同目录上一层
    import os
    path = os.path.join(os.path.dirname(dl.__file__), "..", "metrics.yaml")
    with open(os.path.abspath(path)) as f:
        return yaml.safe_load(f)["metrics"]


def _metric(key: str) -> dict:
    return next(m for m in _load_metrics() if m["key"] == key)


def test_wifi_sync_speed_is_down_is_bad():
    """吞吐量指标：变慢才坏 → 必须 down_is_bad（事故根因锁定）。"""
    m = _metric("wifi_file_sync_speed_p75")
    assert m["direction"] == "down_is_bad", (
        "wifi同步文件速度是吞吐量(KB/s)，越高越好；up_is_bad 会把提速误报为异常"
    )
    assert m["alert_enabled"] is True


def _cfg(direction: str) -> MetricConfig:
    return MetricConfig(
        key="wifi_file_sync_speed_p75", title="wifi同步文件速度P75",
        widget_id=28, widget_type="query_value", tier="P1",
        value_type="latency_pct", direction=direction, threshold={"pct": 0.15},
    )


def test_speed_increase_not_breached():
    """事故现场复现：速度 1107.86 → 1525.68 (+37.71%) 不应告警。"""
    breached, change = _judge(_cfg("down_is_bad"), 1525.68, 1107.86)
    assert breached is False
    assert change == pytest.approx(0.3771, abs=1e-3)


def test_speed_drop_breached():
    """速度暴跌 -30% 是真问题，应告警。"""
    breached, change = _judge(_cfg("down_is_bad"), 775.0, 1107.86)
    assert breached is True
    assert change < 0


def test_speed_small_drop_within_threshold():
    """速度跌 10% 在 15% 阈值内，不告警。"""
    breached, _ = _judge(_cfg("down_is_bad"), 997.0, 1107.86)
    assert breached is False


def test_no_throughput_metric_marked_up_is_bad():
    """通用守卫：键名含 speed / 标题含'速度'的吞吐类指标不得配 up_is_bad。

    防止以后新增吞吐量指标时重蹈覆辙（成功率/延迟/卡顿不在此列）。
    """
    offenders = []
    for m in _load_metrics():
        key = (m.get("key") or "").lower()
        title = m.get("title") or ""
        is_throughput = ("speed" in key) or ("速度" in title)
        # 成功率(rate)、耗时(duration/latency/load/startup)不是吞吐量，排除
        looks_latency = any(
            t in key for t in ("duration", "latency", "load", "startup", "render", "rate")
        )
        if is_throughput and not looks_latency and m.get("direction") == "up_is_bad":
            offenders.append(m["key"])
    assert not offenders, f"吞吐量指标被配成 up_is_bad（越高越坏），方向写反：{offenders}"
