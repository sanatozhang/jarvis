"""预测带引擎单测（design 2026-06-05，回验通过后落地）。

覆盖：band_stats（median/MAD/floor）、judge_band（方向感知穿带）、
方向真相映射、窗口生成。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.coreguard.services.metric_watcher import (
    band_stats,
    judge_band,
    MetricConfig,
)


def _cfg(direction: str, value_type: str = "latency_pct") -> MetricConfig:
    return MetricConfig(
        key="t", title="t", widget_id=1, widget_type="query_value",
        tier="P1", value_type=value_type, direction=direction, threshold=None,
    )


# ── band_stats ──────────────────────────────────────────────────────────
def test_band_stats_median_and_mad():
    # [10,10,10,10,10,40] → median=10，MAD 抗离群点（40 不污染中位数）
    mu, sigma, lo, hi = band_stats([10, 10, 10, 10, 10, 40], "count_pct", k=3,
                                   floor_pp=0.05, floor_rel=0.005)
    assert mu == 10
    assert sigma > 0
    # 离群点 40 没把 μ 拉高（中位数稳）
    assert mu == 10


def test_band_stats_floor_prevents_zero_width():
    # 完全平的历史 → MAD=0 → 带宽靠 floor 兜底，不塌成一个点
    mu, sigma, lo, hi = band_stats([99.6, 99.6, 99.6, 99.6], "percent_pp", k=3,
                                   floor_pp=0.05, floor_rel=0.005)
    assert sigma == pytest.approx(0.05)        # 用绝对 pp floor
    assert hi - lo == pytest.approx(0.3)       # ±3×0.05


def test_band_stats_relative_floor_for_non_percent():
    mu, sigma, lo, hi = band_stats([1000, 1000, 1000], "latency_pct", k=3,
                                   floor_pp=0.05, floor_rel=0.005)
    assert sigma == pytest.approx(5.0)         # 0.005 × 1000


# ── judge_band：方向感知 ──────────────────────────────────────────────────
def test_down_is_bad_breaches_only_lower():
    base = [100, 100, 100, 100, 100]   # μ=100, sigma=floor=0.5
    # 穿下带 → 报
    j = judge_band(_cfg("down_is_bad"), 90, base, k=3, floor_pp=0.05, floor_rel=0.005)
    assert j["breached"] is True
    assert j["sigma_dist"] > 0
    # 穿上带（异常地好）→ 静默
    j2 = judge_band(_cfg("down_is_bad"), 120, base, k=3, floor_pp=0.05, floor_rel=0.005)
    assert j2["breached"] is False


def test_up_is_bad_breaches_only_upper():
    base = [100, 100, 100, 100, 100]
    j = judge_band(_cfg("up_is_bad"), 120, base, k=3, floor_pp=0.05, floor_rel=0.005)
    assert j["breached"] is True
    j2 = judge_band(_cfg("up_is_bad"), 80, base, k=3, floor_pp=0.05, floor_rel=0.005)
    assert j2["breached"] is False


def test_both_breaches_either_side():
    base = [100, 100, 100, 100, 100]
    assert judge_band(_cfg("both"), 120, base, k=3, floor_pp=0.05, floor_rel=0.005)["breached"]
    assert judge_band(_cfg("both"), 80, base, k=3, floor_pp=0.05, floor_rel=0.005)["breached"]


def test_wifi_speed_increase_not_breached():
    """事故场景复现：wifi 速度(down_is_bad)上涨 → 穿上带 → 必须静默，不再误报。"""
    base = [1100, 1110, 1090, 1105, 1108, 1095]   # 历史 ~1100 KB/s
    j = judge_band(_cfg("down_is_bad"), 1525.0, base, k=3, floor_pp=0.05, floor_rel=0.005)
    assert j["breached"] is False                  # 速度上涨是好事，静默


def test_within_band_not_breached():
    base = [1000, 1020, 980, 1010, 990]
    j = judge_band(_cfg("up_is_bad"), 1015, base, k=3, floor_pp=0.05, floor_rel=0.005)
    assert j["breached"] is False


def test_empty_baseline_returns_none():
    assert judge_band(_cfg("up_is_bad"), 100, [], k=3, floor_pp=0.05, floor_rel=0.005) is None
    assert judge_band(_cfg("up_is_bad"), None, [1, 2, 3], k=3, floor_pp=0.05, floor_rel=0.005) is None


# ── 方向真相映射（结构性防方向写反）──────────────────────────────────────
def test_directionality_map():
    from app.coreguard.services.dashboard_loader import _DIRECTIONALITY_MAP
    assert _DIRECTIONALITY_MAP["increase_better"] == "down_is_bad"
    assert _DIRECTIONALITY_MAP["decrease_better"] == "up_is_bad"


def test_reconcile_corrects_conflicting_direction():
    """yaml 写反 → 以 Datadog directionality 为准自动纠正。"""
    from app.coreguard.services.dashboard_loader import _reconcile_direction, MetricConfig as MC
    m = MC(key="wifi", title="wifi", widget_id=28, widget_type="query_value",
           tier="P1", value_type="latency_pct", direction="up_is_bad")  # 故意写反
    req0 = {"comparison": {"directionality": "increase_better"}}
    fixed = _reconcile_direction(m, req0)
    assert fixed == 1
    assert m.direction == "down_is_bad"          # 已纠正
    assert m.dd_directionality == "increase_better"


def test_reconcile_agreement_no_change():
    from app.coreguard.services.dashboard_loader import _reconcile_direction, MetricConfig as MC
    m = MC(key="anr", title="anr", widget_id=2, widget_type="query_value",
           tier="P0", value_type="percent_pp", direction="up_is_bad")
    fixed = _reconcile_direction(m, {"comparison": {"directionality": "decrease_better"}})
    assert fixed == 0
    assert m.direction == "up_is_bad"


# ── 窗口生成 ──────────────────────────────────────────────────────────────
def test_rolling_window_3h():
    from app.coreguard.services.demo_metric import rolling_window
    start, end = rolling_window(datetime(2026, 6, 5, 14, 37), hours=3)
    assert end == datetime(2026, 6, 5, 14, 0)
    assert start == datetime(2026, 6, 5, 11, 0)


def test_same_slot_history_windows():
    from app.coreguard.services.demo_metric import same_slot_history_windows
    cs, ce = datetime(2026, 6, 5, 11, 0), datetime(2026, 6, 5, 14, 0)
    wins = same_slot_history_windows(cs, ce, days_back=14)
    assert len(wins) == 14
    # 第 1 个 = 昨天同时段
    assert wins[0] == (datetime(2026, 6, 4, 11, 0), datetime(2026, 6, 4, 14, 0))
    # 最后 = 14 天前
    assert wins[-1] == (datetime(2026, 5, 22, 11, 0), datetime(2026, 5, 22, 14, 0))
