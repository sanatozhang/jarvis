"""Coreguard 预测带机制 — 历史数据回验（只读，不写库、不发飞书）。

目的（对应设计文档 §6.2 step 1）：
  用真实 Datadog 历史，离线重放"预测带"机制，回答三件事：
    1. k 取多少合理（k=2.5/3/3.5 各自的告警率）
    2. 比旧"单点 SHoW + 固定阈值"机制，误报降了多少
    3. 最大 σ 偏离事件是不是真异常（人工 eyeball）

回验口径（受 Datadog 现实约束，已实测）：
  - 窗口：Datadog timeseries 实际只给 2h 分辨率（无视请求的 3h）→ 回验用 2h 窗，
    比设计的 3h 略噪 → 结论对噪声偏保守（真上线 3h 更干净）。
  - baseline（方案 B）：同 2h-of-day × 近 BASELINE_DAYS 天 → 约 14 个点算 median/MAD。
  - RUM 源仅保留 ~30 天（实测 32d 起空），所以 baseline 天数和回测天数都卡在 30 天内。

用法：  .venv/bin/python scripts/coreguard_band_backtest.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from pathlib import Path

# standalone：手动把根 .env 灌进 os.environ（coreguard fallback 读 CRASHGUARD_* env）
from app.config import PROJECT_ROOT

_env = Path(PROJECT_ROOT) / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import httpx

from app.coreguard.config import get_coreguard_settings
from app.coreguard.services.datadog_scalar import _resolve_template_vars
from app.coreguard.services.dashboard_loader import get_metrics_config

# ── 回验参数 ──────────────────────────────────────────────
H = 3600_000
BUCKET_MS = 2 * H               # Datadog 实际分辨率
HISTORY_DAYS = 30               # 拉多少天历史（RUM 上限 ~30）
BASELINE_DAYS = 14              # 方案 B：同时段近 N 天
MIN_BASELINE_POINTS = 3         # 有效点不足 → 不评估
K_LIST = [2.5, 3.0, 3.5]
N_CONSECUTIVE = 2               # 防抖：连续 N 个窗口同向穿带才算告警
CONCURRENCY = 5


def sigma_floor(value_type: str, mu: float) -> float:
    """带宽地板，防零宽带。百分比类用绝对 pp，其余用相对 μ。"""
    if value_type == "percent_pp":
        return 0.05            # 0.05pp
    return 0.005 * abs(mu)     # latency/count：0.5% of level


async def pull_series(m, start_ms, end_ms):
    """拉一段 2h timeseries → {ts: value}（剔 null）。"""
    s = get_coreguard_settings()
    rq = _resolve_template_vars(m.queries, None)
    body = {"data": {"type": "timeseries_request", "attributes": {
        "formulas": [{"formula": m.formula}],
        "from": int(start_ms), "to": int(end_ms),
        "interval": BUCKET_MS, "queries": rq,
    }}}
    url = f"https://api.{s.datadog_site}/api/v2/query/timeseries"
    headers = {"DD-API-KEY": s.datadog_api_key, "DD-APPLICATION-KEY": s.datadog_app_key,
               "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(url, json=body, headers=headers)
        if r.status_code != 200:
            return {}, f"HTTP {r.status_code}"
    except Exception as e:
        return {}, repr(e)[:80]
    attrs = r.json().get("data", {}).get("attributes", {})
    times = attrs.get("times", [])
    values = attrs.get("values", [])
    vals = values[0] if values else []
    out = {}
    for t, v in zip(times, vals):
        if v is not None:
            # 对齐到 2h 网格，保证跨 chunk 拼接时同 slot 对得上
            out[int(round(t / BUCKET_MS) * BUCKET_MS)] = float(v)
    return out, None


async def pull_full(m, now):
    """分两块拉 HISTORY_DAYS 天（避开 361 点封顶），合并。"""
    mid = now - (HISTORY_DAYS // 2) * 24 * H
    start = now - HISTORY_DAYS * 24 * H
    (a, ea), (b, eb) = await asyncio.gather(
        pull_series(m, start, mid),
        pull_series(m, mid, now),
    )
    merged = {**a, **b}
    return merged, (ea or eb)


def median_mad(xs):
    mu = statistics.median(xs)
    mad = statistics.median([abs(x - mu) for x in xs])
    return mu, mad


def judge_band(value_type, direction, cur, baseline, k):
    """返回 (breached_bad_direction, sigma_distance_or_None)。"""
    if len(baseline) < MIN_BASELINE_POINTS:
        return None, None  # 数据不足
    mu, mad = median_mad(baseline)
    sigma = max(1.4826 * mad, sigma_floor(value_type, mu))
    upper = mu + k * sigma
    lower = mu - k * sigma
    # 方向感知：down_is_bad 只看穿下带；up_is_bad 只看穿上带；both 两侧
    bad = False
    dist = 0.0
    if direction in ("down_is_bad", "both") and cur < lower:
        bad = True
        dist = (lower - cur) / sigma
    if direction in ("up_is_bad", "both") and cur > upper:
        bad = True
        dist = max(dist, (cur - upper) / sigma)
    return bad, (dist if bad else 0.0)


def judge_old(value_type, direction, cur, base_one, threshold):
    """旧机制复刻：单点上周同时段 + 固定阈值。base_one=上周同 slot 单值。"""
    # absolute_threshold（如 hang_rate）：旧逻辑是 cur 与绝对红线比，不走 SHoW
    if value_type == "absolute_threshold":
        red = threshold.get("red")
        if red is None:
            return False
        red = float(red)
        if direction == "up_is_bad":
            return cur >= red
        if direction == "down_is_bad":
            return cur <= red
        return False
    if base_one is None:
        return False
    if value_type == "percent_pp":
        change = cur - base_one
        thr = float(threshold.get("pp", 1.0))
        if direction == "down_is_bad":
            return change <= -thr
        if direction == "up_is_bad":
            return change >= thr
        return abs(change) >= thr
    else:
        if base_one <= 0:
            return False
        change = (cur - base_one) / base_one
        thr = float(threshold.get("pct", 0.20))
        if direction == "down_is_bad":
            return change <= -thr
        if direction == "up_is_bad":
            return change >= thr
        return abs(change) >= thr


def backtest_metric(m, series):
    """对单指标重放新带机制(各 k) + 旧机制。返回统计 dict。"""
    ts_sorted = sorted(series.keys())
    if not ts_sorted:
        return None
    day = 24 * H
    week = 7 * day
    # 回测评估起点：要留够 BASELINE_DAYS 历史
    eval_start = ts_sorted[0] + BASELINE_DAYS * day
    eval_pts = [t for t in ts_sorted if t >= eval_start]

    new_breach = {k: {} for k in K_LIST}   # k -> {ts: sigma_dist}
    new_events = {k: [] for k in K_LIST}   # k -> [(sigma, ts, cur, mu, lower, upper)]
    old_breach = {}                        # ts -> True
    n_eval = 0
    n_insufficient = 0

    is_absolute = (m.value_type == "absolute_threshold")

    for t in eval_pts:
        cur = series[t]
        # absolute_threshold（hang_rate 红线）：新旧都走绝对逻辑，不套带
        if is_absolute:
            n_eval += 1
            if judge_old(m.value_type, m.direction, cur, None, m.threshold):
                for k in K_LIST:
                    new_breach[k][t] = 0.0
                old_breach[t] = True
            continue
        # baseline B：同 2h-of-day 近 N 天
        baseline = [series[t - d * day] for d in range(1, BASELINE_DAYS + 1)
                    if (t - d * day) in series]
        if len(baseline) < MIN_BASELINE_POINTS:
            n_insufficient += 1
            continue
        n_eval += 1
        mu, mad = median_mad(baseline)
        sigma = max(1.4826 * mad, sigma_floor(m.value_type, mu))
        for k in K_LIST:
            bad, dist = judge_band(m.value_type, m.direction, cur, baseline, k)
            if bad:
                new_breach[k][t] = dist
                lower, upper = mu - k * sigma, mu + k * sigma
                new_events[k].append((round(dist, 2), t, round(cur, 3),
                                      round(mu, 3), round(lower, 3), round(upper, 3)))
        # 旧机制：上周同 slot 单点 + 固定阈值
        base_one = series.get(t - week)
        if judge_old(m.value_type, m.direction, cur, base_one, m.threshold):
            old_breach[t] = True

    # 防抖 N=2：连续 BUCKET 都穿带才算一次告警
    def apply_debounce(breach_ts: dict):
        alerts = 0
        bset = set(breach_ts.keys())
        for t in breach_ts:
            if (t - BUCKET_MS) in bset:
                alerts += 1
        return alerts

    new_alerts = {k: apply_debounce(new_breach[k]) for k in K_LIST}
    old_alerts = apply_debounce(old_breach)

    eval_days = (eval_pts[-1] - eval_pts[0]) / day if len(eval_pts) >= 2 else 1
    return {
        "key": m.key, "direction": m.direction, "value_type": m.value_type,
        "n_eval": n_eval, "n_insufficient": n_insufficient,
        "eval_days": max(eval_days, 1),
        "new_alerts": new_alerts, "old_alerts": old_alerts,
        "top_events": {k: sorted(new_events[k], reverse=True)[:3] for k in K_LIST},
    }


def fmt_ts(ms):
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))


async def main():
    cfg = await get_metrics_config(force_reload=True)
    targets = [m for m in cfg.alertable()]
    now = int(time.time() * 1000)
    print(f"回验：{len(targets)} 个 alert_enabled 指标 · 历史 {HISTORY_DAYS}d · "
          f"baseline 同时段近 {BASELINE_DAYS}d · 窗口 2h · 防抖 N={N_CONSECUTIVE}")
    print("=" * 96)

    # 拉数据（小批并发）
    series_map = {}
    for i in range(0, len(targets), CONCURRENCY):
        batch = targets[i:i + CONCURRENCY]
        results = await asyncio.gather(*[pull_full(m, now) for m in batch])
        for m, (s, err) in zip(batch, results):
            series_map[m.key] = s
            if err or len(s) < 20:
                print(f"  ⚠️  {m.key}: 数据少/错误 (n={len(s)}, err={err})")

    # 回测
    stats = []
    for m in targets:
        st = backtest_metric(m, series_map.get(m.key, {}))
        if st:
            stats.append(st)

    # 汇总：各 k 的总告警率
    print("\n【总览：各 k 的告警率 vs 旧机制】")
    print(f"{'k':>5} | {'总告警次数':>10} | {'折合/周':>8} | {'有告警的指标数':>14}")
    print("-" * 56)
    week_factor = {}
    for k in K_LIST:
        total = sum(s["new_alerts"][k] for s in stats)
        total_days = max(statistics.mean([s["eval_days"] for s in stats]), 1)
        per_week = total / total_days * 7
        n_metrics_alerting = sum(1 for s in stats if s["new_alerts"][k] > 0)
        week_factor[k] = per_week
        print(f"{k:>5} | {total:>10} | {per_week:>8.1f} | {n_metrics_alerting:>14}")
    old_total = sum(s["old_alerts"] for s in stats)
    old_days = max(statistics.mean([s["eval_days"] for s in stats]), 1)
    old_metrics = sum(1 for s in stats if s["old_alerts"] > 0)
    print("-" * 56)
    print(f"{'旧':>5} | {old_total:>10} | {old_total/old_days*7:>8.1f} | {old_metrics:>14}")

    # 每指标明细（用推荐 k=3）
    K = 3.0
    print(f"\n【每指标明细 @ k={K}】(新告警次数 / 旧告警次数 · n_eval · 数据不足次数)")
    print("-" * 96)
    for s in sorted(stats, key=lambda x: -x["new_alerts"][K]):
        flag = ""
        if s["new_alerts"][K] == 0 and s["old_alerts"] == 0:
            flag = ""
        elif s["new_alerts"][K] < s["old_alerts"]:
            flag = "  ↓降噪"
        elif s["new_alerts"][K] > s["old_alerts"]:
            flag = "  ↑变多"
        print(f"  {s['key']:34s} {s['direction']:13s} "
              f"新={s['new_alerts'][K]:3d}  旧={s['old_alerts']:3d}  "
              f"n_eval={s['n_eval']:3d}  不足={s['n_insufficient']:3d}{flag}")

    # 最大 σ 偏离事件（人工 eyeball 是不是真异常）
    print(f"\n【最大 σ 偏离 TOP 事件 @ k={K}】(σ偏离 · 时间 · 实际值 · 预测μ · [下带,上带])")
    print("-" * 96)
    all_events = []
    for s in stats:
        for ev in s["top_events"][K]:
            all_events.append((ev[0], s["key"], ev))
    for sigma, key, ev in sorted(all_events, reverse=True)[:15]:
        _, t, cur, mu, lo, hi = ev
        print(f"  {sigma:5.1f}σ  {key:32s} {fmt_ts(t)}  实际={cur:<10g} μ={mu:<10g} [{lo:g}, {hi:g}]")

    print("\n" + "=" * 96)
    print("解读：① 选 k 让'折合/周'落在可接受打扰量；② 对比旧机制看降噪；"
          "③ eyeball TOP 事件确认是真异常不是噪声。")


if __name__ == "__main__":
    asyncio.run(main())
