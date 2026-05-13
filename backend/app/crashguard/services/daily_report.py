"""
Crashguard 早晚报（v2）：4 段 + 分平台 + vs 上周同时段变化率。

底层逻辑：Plaud 用户分布 JP 40% / US 30% / EU 20%，跨时区+跨 weekday 流量差异大。
vs 昨日 24h 会被周末效应（周末用户活跃显著低）污染——周一对比周日天然 +30%，假警频出。
改用「7 天前同时刻往前 24h」做基线，控住 weekday + 时区双重周期，与 hourly alert 同口径。

结构：
  📱 Android / 🍎 iOS / 🐦 Flutter 各自一节，每节 4 段：
    1. 数据快照（主版本 events / 全版本 events / 平均 crash-free 率）
    2. 新增 / 突增（>= +10% vs 上周同时段，或 is_new_in_version）
    3. 下降（<= -10% vs 上周同时段）
    4. Top 5（按 crash_free_impact_score）

外部入口：
- compose_report(report_type, target_date) → (markdown, payload)
- send_daily_report(report_type) → 写库 + 推飞书
"""
from __future__ import annotations

import json as _json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import (
    CrashAnalysis,
    CrashDailyReport,
    CrashIssue,
    CrashSnapshot,
)
from app.db.database import get_session

logger = logging.getLogger("crashguard.daily_report")

# fire-and-forget 后台任务强引用集合——防止 asyncio.create_task 返回值丢失被 GC 回收
_BG_TASKS: set = set()


def _spawn_bg(coro, name: str = "daily-report-bg"):
    import asyncio as _asyncio
    task = _asyncio.create_task(coro, name=name)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


REPORT_TYPES = ("morning", "evening")


def _thresholds() -> Tuple[float, float, int]:
    """读取 config 中的 surge/drop 阈值 + events 下限，默认 ±10% / 100 events。"""
    s = get_crashguard_settings()
    return (
        float(getattr(s, "daily_surge_threshold", 0.10) or 0.10),
        float(getattr(s, "daily_drop_threshold", -0.10) or -0.10),
        int(getattr(s, "daily_attention_min_events", 100) or 100),
    )

PLATFORM_DISPLAY = [
    # iOS 用户量更大，置顶；Android 次之
    ("IOS", "🍎 iOS"),
    ("ANDROID", "📱 Android"),
    # UNKNOWN 桶（FLUTTER 无 RUM 采样的低频 issue）一并忽略，不展示
]

# C 路线：早晚报只关注 fatal（App 真挂/卡），non_fatal 量大噪音多，不入日报
# 业务失败明细去首页大盘看（双卡 + 列表），日报保持精简
FATALITY_DISPLAY = [
    ("fatal", "🔴 严重崩溃（App 挂/卡）"),
]


def _resolve_real_os(raw_platform: str, top_os: str) -> Optional[str]:
    """
    把 issue 归类到真实 OS：
    - ANDROID / IOS（原生 SDK 上报） → 直接对应
    - FLUTTER（Dart 业务代码，跨 OS 运行）：解析 top_os
        · iOS / iPadOS 起首占比最高 → IOS
        · Android 起首占比最高 → ANDROID
        · 无 top_os → None（直接忽略，低频 issue 不上报）
    - BROWSER 等其他 → None（忽略）
    """
    p = (raw_platform or "").upper().strip()
    if p == "ANDROID":
        return "ANDROID"
    if p == "IOS":
        return "IOS"
    if p == "FLUTTER":
        if not top_os:
            return None  # 低频 FLUTTER issue 无 RUM 采样，直接忽略
        head = top_os.strip().split(",")[0].strip().lower()
        if head.startswith("ipados") or head.startswith("ios"):
            return "IOS"
        if head.startswith("android"):
            return "ANDROID"
        return None
    return None


def _frontend_issue_url(issue_id: str) -> str:
    base = get_crashguard_settings().frontend_base_url or "http://localhost:3000"
    return f"{base.rstrip('/')}/crashguard?issue={issue_id}"


def _parse_top_app_version(s: str) -> List[Tuple[str, float]]:
    """
    解析 crash_issues.top_app_version：
        "3.15.1-630 (87.4%), 3.16.0-634 (8.2%), 3.14.0-620 (2.8%)"
    返回 [(version, pct), ...]，pct 已是百分数。无法解析返回 []。
    """
    import re
    if not s:
        return []
    out: List[Tuple[str, float]] = []
    for chunk in s.split(","):
        m = re.match(r"\s*(\S.*?)\s*\(\s*([\d.]+)\s*%\s*\)\s*$", chunk)
        if m:
            out.append((m.group(1).strip(), float(m.group(2))))
    return out


def _baseline_min_for_pct() -> int:
    """基线 events 低于此值时，% 噪声太大，不进入 surge attention（只允许绝对量级触发）。"""
    s = get_crashguard_settings()
    return int(getattr(s, "daily_baseline_min_events_for_pct", 500) or 500)


def _delta_pct(today: int, baseline: Optional[int]) -> Optional[float]:
    """ratio = (today - baseline) / baseline；基线为 None / 0 时返回 None。

    baseline 含义：默认是「上周同时段 24h」(SHoW-24h)；如 SHoW 数据缺失，调用方可
    传 fallback 数据。返回 None = 无法计算（基线缺失），不应作为告警依据。
    """
    if baseline is None or baseline == 0:
        return None
    return (today - baseline) / baseline


def _classify_platform(raw: str) -> Optional[str]:
    """旧函数（保留兼容）。新逻辑用 _resolve_real_os(raw_platform, top_os)。"""
    p = (raw or "").upper().strip()
    if p in ("ANDROID", "IOS", "FLUTTER"):
        return p
    return None  # BROWSER 等忽略


def _line_for_issue(
    issue_id: str,
    title: str,
    events_today: int,
    delta_ratio: Optional[float],
    extra: str = "",
    is_new_in_version: bool = False,
) -> str:
    url = _frontend_issue_url(issue_id)
    title_short = (title or "")[:70]
    if delta_ratio is None:
        delta_str = "🆕新版" if is_new_in_version else "—"
    else:
        sign = "+" if delta_ratio >= 0 else ""
        delta_str = f"{sign}{delta_ratio * 100:.0f}%"
    extra_str = f" · {extra}" if extra else ""
    return f"- **{events_today:,}** events ({delta_str}){extra_str} · [{title_short}]({url})"


async def compose_report(
    report_type: str,
    target_date: date | None = None,
    top_n: int = 5,
    view_window_hours: int = 24,
) -> Tuple[str, Dict[str, Any]]:
    """生成 4 段 + 分平台 markdown 报告。

    view_window_hours: **每 issue 的 events 数字展示窗口**（24/168/336/720 = 1d/7d/14d/30d）。
        24 = 默认，与 cron 实际发送的报告一致。
        > 24 = 仅"渲染时口径"，把每 issue 的 events 替换为跨 N 天 CrashSnapshot sum，
              方便回看长期累计；**不影响 SHoW-24h 基线对比逻辑**（基线本身就是 24h vs 24h）。
    """
    if report_type not in REPORT_TYPES:
        raise ValueError(f"invalid report_type: {report_type}")
    if target_date is None:
        target_date = date.today()
    # 归一窗口档位
    if view_window_hours not in (24, 168, 336, 720):
        view_window_hours = 24
    # SHoW-24h 基线：上周同 weekday 的 24h 快照（控时区+周内双周期偏置）
    baseline_date = target_date - timedelta(days=7)
    # 业务硬约束：每平台 Top 上限 5（不可配置，避免 UI 上限漂移）
    top_n = min(max(1, int(top_n)), 5)
    # 阈值从 config 读（可在 config.yaml 覆盖）
    surge_threshold, drop_threshold, attention_min_events = _thresholds()

    async with get_session() as session:
        # 今日所有 snap join issue
        today_rows = (await session.execute(
            select(CrashSnapshot, CrashIssue)
            .join(CrashIssue, CrashIssue.datadog_issue_id == CrashSnapshot.datadog_issue_id)
            .where(CrashSnapshot.snapshot_date == target_date)
        )).all()

        # 基线 snap → dict（DB fallback：实时拉失败时降级用）
        baseline_rows = (await session.execute(
            select(CrashSnapshot.datadog_issue_id, CrashSnapshot.events_count)
            .where(CrashSnapshot.snapshot_date == baseline_date)
        )).all()
        baseline_events: Dict[str, int] = {
            r[0]: int(r[1] or 0) for r in baseline_rows
        }

        # view_window_hours > 24：批量算每 issue 的跨 N 天 events sum，覆盖 snap.events_count
        # 注意：snap 是 SQLA 对象，session 关闭后改 .events_count 不会持久化，仅影响渲染。
        if view_window_hours > 24 and today_rows:
            from sqlalchemy import func
            days = view_window_hours // 24
            window_start = target_date - timedelta(days=days - 1)
            issue_ids = [snap.datadog_issue_id for snap, _ in today_rows]
            agg_rows = (await session.execute(
                select(
                    CrashSnapshot.datadog_issue_id,
                    func.sum(CrashSnapshot.events_count).label("ev"),
                )
                .where(
                    CrashSnapshot.datadog_issue_id.in_(issue_ids),
                    CrashSnapshot.snapshot_date >= window_start,
                    CrashSnapshot.snapshot_date <= target_date,
                )
                .group_by(CrashSnapshot.datadog_issue_id)
            )).all()
            window_events: Dict[str, int] = {r[0]: int(r[1] or 0) for r in agg_rows}
            for snap, _ in today_rows:
                if snap.datadog_issue_id in window_events:
                    snap.events_count = window_events[snap.datadog_issue_id]

    # 拉每个平台的 24h 总 sessions + distinct crash sessions（含 ANR），
    # 用于以 Datadog 官方口径算 crash-free rate；失败返回空 dict 不致命。
    s_cfg = get_crashguard_settings()
    # 早晚报差异化窗口（A+B 方案）：
    #   morning → datadog_window_hours (24h) 昨日总览
    #   evening → evening_window_hours (10h) 日内增量 vs 上周同段
    if report_type == "evening":
        data_window_hours = max(1, int(getattr(s_cfg, "evening_window_hours", 10) or 10))
    else:
        data_window_hours = max(1, int(s_cfg.datadog_window_hours or 24))
    total_sessions_by_plat: Dict[str, int] = {}
    distinct_crash_sessions_by_plat: Dict[str, int] = {}
    crash_breakdown_by_plat: Dict[str, Dict[str, int]] = {}
    # SHoW 实时双窗口拉数（fatal/non_fatal × today/baseline = 4 次，含 5min 缓存）
    # today    窗口 = [now-Nh, now]
    # baseline 窗口 = [now-(168+N)h, now-168h]   ← 7 天前同时刻往前 N h
    # 关键收益：严格对齐 weekday + 时区双周期，规避 vs 昨日的周末效应假警。
    realtime_today_events: Dict[str, int] = {}
    realtime_baseline_events: Dict[str, int] = {}
    # 双窗口对照所需：baseline sessions by platform（today 已在上面拉过）
    baseline_sessions_by_plat: Dict[str, int] = {}
    if s_cfg.datadog_api_key:
        try:
            from app.crashguard.services.datadog_client import DatadogClient
            client = DatadogClient(
                api_key=s_cfg.datadog_api_key,
                app_key=s_cfg.datadog_app_key,
                site=s_cfg.datadog_site,
            )
            raw_total = await client.count_sessions_by_platform(window_hours=data_window_hours)
            total_sessions_by_plat = {k.upper(): v for k, v in (raw_total or {}).items()}
            raw_crash = await client.count_distinct_crash_sessions_by_platform(
                window_hours=data_window_hours
            )
            distinct_crash_sessions_by_plat = {k.upper(): v for k, v in (raw_crash or {}).items()}
            raw_breakdown = await client.fetch_crash_breakdown_by_platform(
                window_hours=data_window_hours
            )
            crash_breakdown_by_plat = {k.upper(): v for k, v in (raw_breakdown or {}).items()}

            # dual-window × dual-fatality 拉 events
            import time as _t
            now_ms = int(_t.time() * 1000)
            win_ms = data_window_hours * 3600 * 1000
            for q in (s_cfg.datadog_query_fatal, s_cfg.datadog_query_nonfatal):
                try:
                    today_pull = await client.list_issues_for_window(
                        start_ms=now_ms - win_ms, end_ms=now_ms,
                        tracks=s_cfg.datadog_tracks, query=q,
                    )
                    for it in today_pull:
                        iid = it.get("id") or ""
                        if iid:
                            realtime_today_events[iid] = int(
                                it.get("attributes", {}).get("events_count", 0) or 0
                            )
                    # SHoW-24h 基线：7 天前同时刻往前 24h
                    week_ago_end_ms = now_ms - 7 * 24 * 3600 * 1000
                    week_ago_start_ms = week_ago_end_ms - win_ms
                    baseline_pull = await client.list_issues_for_window(
                        start_ms=week_ago_start_ms, end_ms=week_ago_end_ms,
                        tracks=s_cfg.datadog_tracks, query=q,
                    )
                    for it in baseline_pull:
                        iid = it.get("id") or ""
                        if iid:
                            realtime_baseline_events[iid] = int(
                                it.get("attributes", {}).get("events_count", 0) or 0
                            )
                except Exception:
                    logger.exception("dual-window pull failed for query=%s (non-fatal)", q)
            # baseline sessions（上周同 N 小时段）
            try:
                _week_ago_end = now_ms - 7 * 24 * 3600 * 1000
                _week_ago_start = _week_ago_end - win_ms
                raw_base_sess = await client.count_sessions_in_window(
                    start_ms=_week_ago_start, end_ms=_week_ago_end,
                )
                baseline_sessions_by_plat = {
                    k.upper(): v for k, v in (raw_base_sess or {}).items()
                }
            except Exception:
                logger.exception("baseline sessions pull failed (non-fatal)")
        except Exception:
            logger.exception("count_sessions failed (non-fatal)")

    # 用实时窗口数据覆盖 baseline_events
    if realtime_baseline_events:
        baseline_events = realtime_baseline_events
    elif report_type == "evening":
        # 晚报 10h 窗口下，CrashSnapshot 日级 baseline 颗粒度不对齐，强制清空避免假警
        # 实时拉失败时退化到"无基线"，宁可少报警也别误报
        baseline_events = {}
    # 方案 A：用实时窗口数据覆盖 today snapshot 的 events_count（in-memory mutation，session 已关）
    # 这样后续所有 snap.events_count 引用都自动用对齐窗口数据，不必逐处改写。
    #
    # 治本（2026-05-13 用户实测发现 bug）：原代码只在 `rt is not None` 时覆盖，issue 不
    # 在 realtime（被 Datadog Top100 截断 / 当前窗口实际为 0）时仍保留 DB 旧值（可能是
    # 24h 全量或回填数据）→ 速报里展示的 events vs SHoW % 口径混杂。
    # 修：realtime 拉成功后，**所有 today_rows 的 events_count 一律按 realtime 重写**，
    # 不在 realtime 内的强制置 0（10h 窗口内确实没产生 events）。
    if realtime_today_events:
        for snap, _ in today_rows:
            rt = realtime_today_events.get(snap.datadog_issue_id)
            snap.events_count = int(rt) if rt is not None else 0

    # 按真实 OS 分桶（FLUTTER 按 top_os 重新归类，无 top_os 进 UNKNOWN）
    by_platform: Dict[str, List[Tuple[CrashSnapshot, CrashIssue]]] = defaultdict(list)
    for snap, issue in today_rows:
        plat = _resolve_real_os(
            issue.platform or "",
            getattr(issue, "top_os", "") or "",
        )
        if plat is None:
            continue  # BROWSER 等忽略
        by_platform[plat].append((snap, issue))

    # 输出
    s = get_crashguard_settings()
    is_evening = report_type == "evening"
    # 卡片/markdown 标题（晚报改名"速报"，与"日报"对仗，明确区分）
    title = (
        f"🌇 Crashguard 速报"
        if is_evening
        else "🌅 Crashguard 日报"
    )
    # 顶置口径 banner（quote 形式让群/web 端醒目展示，2 秒可识别）
    if view_window_hours > 24:
        view_label = {168: "近 7 天", 336: "近 14 天", 720: "近 30 天"}.get(view_window_hours, "")
        scope_banner = (
            f"> 📊 **展示窗口**：{view_label}（CrashSnapshot 跨日累计） · "
            f"基线对比仍按发送时 SHoW 口径"
        )
    elif is_evening:
        scope_banner = (
            f"> 📊 **数据口径**：过去 **{data_window_hours}h**（日内增量） · "
            f"基线：**上周同 weekday 同 {data_window_hours}h 段**（SHoW-{data_window_hours}h）"
        )
    else:
        scope_banner = (
            f"> 📊 **数据口径**：过去 **24h**（昨日总览） · "
            f"基线：**上周同 weekday 同 24h 段**（SHoW-24h）"
        )
    window_caption = scope_banner
    lines: List[str] = [
        f"# {title} — {target_date.isoformat()}",
        window_caption,
        "",
    ]

    # 双窗口对照：让用户一眼看 sessions + fatal events 是否真增长
    # 早报 24h（vs 上周同 weekday 24h）、速报 10h（vs 上周同 10h）都展示
    if (
        total_sessions_by_plat or baseline_sessions_by_plat
        or realtime_today_events or realtime_baseline_events
    ):
        from datetime import timedelta as _td
        base_date = target_date - _td(days=7)
        today_fatal_total = sum(realtime_today_events.values()) if realtime_today_events else 0
        base_fatal_total = sum(realtime_baseline_events.values()) if realtime_baseline_events else 0

        def _delta_str(t: int, b: int) -> str:
            if b == 0:
                return "—" if t == 0 else f"+{t} (基线 0)"
            pct = (t - b) / b * 100
            sign = "+" if pct >= 0 else ""
            return f"{sign}{pct:.0f}%"

        cmp_lines = [
            "",
            f"### 📊 双窗口对照（{data_window_hours}h，今天 {target_date.isoformat()} vs 上周 {base_date.isoformat()}）",
            "",
            "| 平台 | 今天 sessions | 上周 sessions | Δ sessions | 今天 fatal events | 上周 fatal events | Δ events |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        # 平台 fatal 分桶需要从 realtime dict 反查 issue.platform
        # 颗粒度：只展示三平台（Android/iOS/Others）合并
        today_fatal_by_plat: Dict[str, int] = {"ANDROID": 0, "IOS": 0, "OTHER": 0}
        base_fatal_by_plat: Dict[str, int] = {"ANDROID": 0, "IOS": 0, "OTHER": 0}
        # 走 today_rows 已 normalized 的 platform
        id_to_plat: Dict[str, str] = {}
        for snap, issue in today_rows:
            p = (issue.platform or "").upper()
            if p == "FLUTTER":
                # 用 top_os 还原真平台
                tp = _resolve_real_os("FLUTTER", getattr(issue, "top_os", "") or "")
                p = (tp or "OTHER").upper()
            id_to_plat[snap.datadog_issue_id] = p if p in ("ANDROID", "IOS") else "OTHER"
        for iid, ev in (realtime_today_events or {}).items():
            today_fatal_by_plat[id_to_plat.get(iid, "OTHER")] += int(ev)
        for iid, ev in (realtime_baseline_events or {}).items():
            base_fatal_by_plat[id_to_plat.get(iid, "OTHER")] += int(ev)

        for plat_key in ("ANDROID", "IOS"):
            t_sess = total_sessions_by_plat.get(plat_key, 0)
            b_sess = baseline_sessions_by_plat.get(plat_key, 0)
            t_fatal = today_fatal_by_plat[plat_key]
            b_fatal = base_fatal_by_plat[plat_key]
            cmp_lines.append(
                f"| {plat_key} | {t_sess} | {b_sess} | {_delta_str(t_sess, b_sess)} | "
                f"{t_fatal} | {b_fatal} | {_delta_str(t_fatal, b_fatal)} |"
            )
        # 合计行
        t_sess_all = sum(total_sessions_by_plat.get(k, 0) for k in ("ANDROID", "IOS"))
        b_sess_all = sum(baseline_sessions_by_plat.get(k, 0) for k in ("ANDROID", "IOS"))
        cmp_lines.append(
            f"| **合计** | **{t_sess_all}** | **{b_sess_all}** | **{_delta_str(t_sess_all, b_sess_all)}** | "
            f"**{today_fatal_total}** | **{base_fatal_total}** | **{_delta_str(today_fatal_total, base_fatal_total)}** |"
        )
        cmp_lines.append("")
        cmp_lines.append(
            "> 💡 看法：fatal Δ 大幅高于 sessions Δ → crash rate 真恶化（不是用户量增加）"
        )
        cmp_lines.append("")
        lines.extend(cmp_lines)

    payload: Dict[str, Any] = {
        "report_date": target_date.isoformat(),
        "report_type": report_type,
        # 卡片头部 banner 用：标注本次报告的实际拉取窗口
        "data_window_hours": data_window_hours,
        "platforms": {},
    }

    if not today_rows:
        lines.append("> 今日暂无快照数据。")
        text = "\n".join(lines)
        payload["new_count"] = payload["regression_count"] = payload["surge_count"] = 0
        payload["top_n"] = 0
        return text, payload

    total_new = total_surge = total_drop = total_top = 0
    # 关注点摘要：跨平台收集 anomalies（新增 / 突增 / 下降）
    attn_news: List[Dict[str, Any]] = []
    attn_surges: List[Dict[str, Any]] = []
    attn_drops: List[Dict[str, Any]] = []

    # C 路线扩 auto-PR 池：跨平台收集 Top10 fatal + Top10 non_fatal + 全部新增（含 non_fatal）
    # 这部分独立于日报渲染——日报只展示 fatal，但 auto-PR 池要更广，
    # 让 non_fatal Top10 / 新增 non_fatal 也能享受"自动分析→自动建 PR"待遇。
    auto_pr_candidates: set = set()
    fatal_pool_xplat: List[Tuple[CrashSnapshot, CrashIssue]] = []
    nonfatal_pool_xplat: List[Tuple[CrashSnapshot, CrashIssue]] = []
    for plat_key, _ in PLATFORM_DISPLAY:
        for snap, issue in by_platform.get(plat_key, []):
            f = (getattr(issue, "fatality", "") or "").lower()
            if f == "non_fatal":
                nonfatal_pool_xplat.append((snap, issue))
            else:
                fatal_pool_xplat.append((snap, issue))  # legacy unknown 兜底归 fatal
            if snap.is_new_in_version:
                auto_pr_candidates.add(snap.datadog_issue_id)
    fatal_pool_xplat.sort(
        key=lambda r: (
            float(r[0].crash_free_impact_score or 0.0),
            int(r[0].events_count or 0),
        ),
        reverse=True,
    )
    nonfatal_pool_xplat.sort(
        key=lambda r: (
            float(r[0].crash_free_impact_score or 0.0),
            int(r[0].events_count or 0),
        ),
        reverse=True,
    )
    AUTO_PR_TOP_N = 10
    for snap, _ in fatal_pool_xplat[:AUTO_PR_TOP_N]:
        auto_pr_candidates.add(snap.datadog_issue_id)
    for snap, _ in nonfatal_pool_xplat[:AUTO_PR_TOP_N]:
        auto_pr_candidates.add(snap.datadog_issue_id)

    for plat_key, plat_label in PLATFORM_DISPLAY:
        all_plat_rows = by_platform.get(plat_key, [])
        if not all_plat_rows:
            continue

        # C 路线：日报只看 fatal——legacy 行 fatality 缺失/unknown 兜底归 fatal（pre-C 默认就是"崩溃"）
        plat_fatality_buckets: Dict[str, List[Tuple[CrashSnapshot, CrashIssue]]] = defaultdict(list)
        for snap, issue in all_plat_rows:
            f = (getattr(issue, "fatality", "") or "").lower()
            if f not in ("fatal", "non_fatal"):
                f = "fatal"  # legacy fallback
            plat_fatality_buckets[f].append((snap, issue))

        # 日报只渲染 fatal——直接收敛 plat_rows 到 fatal 桶
        plat_rows = plat_fatality_buckets.get("fatal", [])
        if not plat_rows:
            continue  # 该平台无 fatal issue，跳过整段

        # ── Section 1：数据快照（仅 fatal 池）──────────────────────────
        # 真实版本分布在 crash_issues.top_app_version（RUM Events 按 application.version
        # 聚合的真分布）。snapshot.app_version 只是 last_seen_version 的拷贝（误导，禁用）。
        # 策略：跨 issue 加权——以每个 issue 的 events_count 为权重，把 top_app_version 解析后
        # 按版本重新聚合，得到该平台真实的"主力版本"。
        rows = plat_rows
        events_total = sum(int(snap.events_count or 0) for snap, _ in rows)
        sessions_total = sum(int(snap.sessions_affected or 0) for snap, _ in rows)
        impact_total = sum(float(snap.crash_free_impact_score or 0.0) for snap, _ in rows)
        issue_count = len(rows)

        # 收集 first/last seen 版本范围（fallback 用）
        first_versions: List[str] = []
        last_versions: List[str] = []
        for _, issue in rows:
            fv = (issue.first_seen_version or "").strip()
            lv = (issue.last_seen_version or "").strip()
            if fv:
                first_versions.append(fv)
            if lv:
                last_versions.append(lv)
        oldest_first = min(first_versions) if first_versions else "?"
        newest_last = max(last_versions) if last_versions else "?"

        # 跨 issue 按 events 加权聚合 top_app_version
        weighted_events_by_ver: Dict[str, float] = defaultdict(float)
        issues_with_dist = 0
        issues_total = len(rows)
        for snap, issue in rows:
            ev = int(snap.events_count or 0)
            dist = _parse_top_app_version(getattr(issue, "top_app_version", "") or "")
            if not dist or ev == 0:
                continue
            issues_with_dist += 1
            for ver, pct in dist:
                weighted_events_by_ver[ver] += ev * (pct / 100.0)

        if weighted_events_by_ver:
            sorted_vers = sorted(weighted_events_by_ver.items(), key=lambda kv: kv[1], reverse=True)
            main_version, main_events_weighted = sorted_vers[0]
            main_pct_of_total = (
                main_events_weighted / events_total * 100.0 if events_total > 0 else 0.0
            )
            top3_vers = sorted_vers[:3]
        else:
            main_version = newest_last
            main_events_weighted = 0.0
            main_pct_of_total = 0.0
            top3_vers = []

        # 旧字段兼容（payload）
        events_main = int(main_events_weighted)
        sessions_main = sessions_total
        version_count = len(weighted_events_by_ver) or len(set(first_versions + last_versions))

        # 渲染本平台标题（§1 数据快照在标题之后）
        lines.append(f"## {plat_label}")
        lines.append("")

        # § 1 — 数据快照
        # crash-free 用 Datadog 官方口径（distinct crash sessions / total sessions），含 ANR
        total_sessions = total_sessions_by_plat.get(plat_key, 0)
        distinct_crash = distinct_crash_sessions_by_plat.get(plat_key, 0)
        crash_free_str = ""
        if total_sessions > 0 and distinct_crash >= 0:
            cf_rate = max(0.0, min(1.0, 1 - distinct_crash / total_sessions))
            crash_free_str = (
                f" · **Crash-free {cf_rate * 100:.2f}%** "
                f"({distinct_crash:,} crash / {total_sessions:,} sessions)"
            )
        lines.append("### 📊 数据快照")
        lines.append(
            f"- 事件总数 **{events_total:,}** · 影响 **{sessions_total:,}** sessions（issue 累加）· "
            f"综合影响分 **{impact_total:,.1f}** · {issue_count} 个 issue{crash_free_str}"
        )
        # 错误事件类型分布（仅供参考，按 error 事件计数；非 session 计数）
        bd = crash_breakdown_by_plat.get(plat_key) or {}
        if bd:
            parts: List[str] = []
            if bd.get("native_crash"):
                parts.append(f"Native crash **{bd['native_crash']:,}**")
            if bd.get("anr"):
                parts.append(f"ANR **{bd['anr']:,}**")
            if bd.get("app_hang"):
                parts.append(f"App Hang **{bd['app_hang']:,}**")
            if parts:
                lines.append(f"- 错误事件分布（24h）：{' · '.join(parts)}")
        # 版本范围（基于 first_seen / last_seen）
        if oldest_first == newest_last:
            range_str = f"`{newest_last}`"
        else:
            range_str = f"`{oldest_first}` → `{newest_last}`"
        lines.append(f"- 版本跨度 {range_str}")

        # 真实主力版本（基于 RUM Events 加权聚合）
        if top3_vers:
            top3_str = " · ".join(
                f"**{v}** {(w / events_total * 100):.1f}%"
                for v, w in top3_vers if events_total > 0
            )
            lines.append(
                f"- 主力版本（按 events 加权）{top3_str} _（基于 {issues_with_dist}/{issues_total} 个 issue 的 RUM 真实采样）_"
            )
        else:
            lines.append(
                f"- _⚠️ 主力版本数据待补：{issues_total} 个 issue 均无 RUM 分布采样（首次 AI 分析后自动写入 top_app_version）_"
            )
        lines.append("")

        # ── §2-4 按 fatality 分桶渲染 ──────────────────
        # C 路线核心：fatal / non_fatal 各自独立 Top N + 突增/下降，互不挤压。
        plat_surge_total = 0
        plat_drop_total = 0
        plat_top_total = 0
        plat_new_total = 0
        plat_payload_buckets: Dict[str, Dict[str, int]] = {}

        for fkey, flabel in FATALITY_DISPLAY:
            f_rows = plat_fatality_buckets.get(fkey, [])
            if not f_rows:
                continue

            # § 2 - 突增 / 新增（per fatality）
            baseline_min_pct = _baseline_min_for_pct()
            surges: List[Tuple[CrashSnapshot, CrashIssue, Optional[float]]] = []
            for snap, issue in f_rows:
                events_today = int(snap.events_count or 0)
                yt = baseline_events.get(snap.datadog_issue_id)
                delta = _delta_pct(events_today, yt)
                is_new = bool(snap.is_new_in_version)
                # 小基数过滤：baseline < N 时 % 噪声过大（500 ev → 1000 ev 看着 +100%
                # 但绝对增量才 500，远低于 4000+ ev 真信号）。要么用绝对增量，要么忽略
                base_too_small = (yt is None or yt < baseline_min_pct)
                is_surge = (
                    delta is not None
                    and delta >= surge_threshold
                    and events_today >= attention_min_events
                    and not base_too_small
                )
                if is_new or is_surge:
                    surges.append((snap, issue, delta))
                    bucket = attn_news if is_new else attn_surges
                    bucket.append({
                        "issue_id": snap.datadog_issue_id,
                        "title": issue.title or "",
                        "platform": plat_label,
                        "fatality": fkey,
                        "events": events_today,
                        "delta": delta,
                    })
            surges.sort(
                key=lambda t: (
                    -(int(t[0].events_count or 0)),
                    -(t[2] if t[2] is not None else 1e9),
                )
            )

            # § 3 - 下降（per fatality）
            drops: List[Tuple[CrashSnapshot, CrashIssue, float]] = []
            for snap, issue in f_rows:
                events_today = int(snap.events_count or 0)
                yt = baseline_events.get(snap.datadog_issue_id)
                delta = _delta_pct(events_today, yt)
                yt_int = int(yt or 0)
                yt_satisfies = yt_int >= attention_min_events
                today_satisfies = events_today >= attention_min_events
                if (
                    delta is not None
                    and delta <= drop_threshold
                    and (today_satisfies or yt_satisfies)
                ):
                    drops.append((snap, issue, delta))
                    attn_drops.append({
                        "issue_id": snap.datadog_issue_id,
                        "title": issue.title or "",
                        "platform": plat_label,
                        "fatality": fkey,
                        "events": events_today,
                        "delta": delta,
                    })
            drops.sort(key=lambda t: t[2])

            # § 4 - Top N（per fatality）
            top5 = sorted(
                f_rows,
                key=lambda r: (float(r[0].crash_free_impact_score or 0.0), int(r[0].events_count or 0)),
                reverse=True,
            )[:top_n]

            # 渲染本 fatality 段
            f_events = sum(int(s.events_count or 0) for s, _ in f_rows)
            lines.append(f"### {flabel} — {f_events:,} events / {len(f_rows)} issue")
            lines.append("")

            # § 2 渲染
            lines.append(
                f"#### 🆕 新增 / 突增（>= +{int(surge_threshold * 100)}% vs 上周同时段，"
                f"突增需 events ≥ {attention_min_events}）"
            )
            if not surges:
                lines.append("- _无_")
            else:
                for snap, issue, delta in surges[:5]:
                    tags: List[str] = []
                    if snap.is_new_in_version:
                        tags.append("新版首现")
                    if snap.is_regression:
                        tags.append("回归")
                    tag_str = ", ".join(tags)
                    lines.append(_line_for_issue(
                        snap.datadog_issue_id,
                        issue.title or "",
                        int(snap.events_count or 0),
                        delta,
                        extra=tag_str,
                        is_new_in_version=bool(snap.is_new_in_version),
                    ))
            lines.append("")

            # § 3 渲染
            lines.append(f"#### 📉 下降（<= {int(drop_threshold * 100)}% vs 上周同时段）")
            if not drops:
                lines.append("- _无_")
            else:
                for snap, issue, delta in drops[:5]:
                    lines.append(_line_for_issue(
                        snap.datadog_issue_id,
                        issue.title or "",
                        int(snap.events_count or 0),
                        delta,
                    ))
            lines.append("")

            # § 4 渲染
            lines.append(f"#### 🔥 Top {len(top5)}")
            for i, (snap, issue) in enumerate(top5, 1):
                events_today = int(snap.events_count or 0)
                yt = baseline_events.get(snap.datadog_issue_id)
                delta = _delta_pct(events_today, yt)
                lines.append(_line_for_issue(
                    snap.datadog_issue_id,
                    f"{i}. {issue.title or ''}",
                    events_today,
                    delta,
                    is_new_in_version=bool(snap.is_new_in_version),
                ))
            lines.append("")

            plat_new_total += sum(1 for s, _, d in surges if s.is_new_in_version)
            plat_surge_total += sum(1 for s, _, d in surges if not s.is_new_in_version)
            plat_drop_total += len(drops)
            plat_top_total += len(top5)
            plat_payload_buckets[fkey] = {
                "events": f_events,
                "issue_count": len(f_rows),
                "surge_count": len(surges),
                "drop_count": len(drops),
                "top_count": len(top5),
            }

        lines.append("---")
        lines.append("")

        total_new += plat_new_total
        total_surge += plat_surge_total
        total_drop += plat_drop_total
        total_top += plat_top_total

        payload["platforms"][plat_key] = {
            "main_version": main_version,
            "events_main": events_main,
            "sessions_main": sessions_main,
            "events_total": events_total,
            "sessions_total": sessions_total,
            "version_count": version_count,
            "impact_total": round(impact_total, 1),
            "fatality_buckets": plat_payload_buckets,
            "surge_count": plat_surge_total,
            "drop_count": plat_drop_total,
            "top_count": plat_top_total,
        }

    # ── 顶部摘要 + 关注点 ─────────────────────────────
    sigma = (
        f"> Σ 新增 **{total_new}** · 突增 **{total_surge}** · "
        f"下降 **{total_drop}** · Top 总览 **{total_top}**"
    )

    # C 路线：日报只关注 fatal——non_fatal 量大噪音多，全量去首页大盘看
    fatal_news = [x for x in attn_news if x.get("fatality") == "fatal"]
    fatal_surges = [x for x in attn_surges if x.get("fatality") == "fatal"]
    fatal_drops = [x for x in attn_drops if x.get("fatality") == "fatal"]

    attn_lines: List[str] = ["## ✨ 今日关注点"]
    has_fatal_anomaly = bool(fatal_news or fatal_surges or fatal_drops)

    if not has_fatal_anomaly:
        attn_lines.append("")
        attn_lines.append("> 🌿 **数据平稳，安全无虞** — 无新增崩溃，无 ±10% 以上波动。")
    else:
        if fatal_news:
            attn_lines.append("")
            attn_lines.append(f"### 🆕 新增 ({len(fatal_news)} 项)")
            for item in sorted(fatal_news, key=lambda x: -x["events"])[:5]:
                url = _frontend_issue_url(item["issue_id"])
                title_short = item["title"][:70]
                attn_lines.append(
                    f"- [{item['platform']}] **{item['events']:,}** events · "
                    f"[{title_short}]({url})"
                )
        if fatal_surges:
            attn_lines.append("")
            attn_lines.append(f"### 📈 突增 (>= +10% vs 上周同时段, {len(fatal_surges)} 项)")
            for item in sorted(fatal_surges, key=lambda x: -(x["delta"] or 0))[:5]:
                url = _frontend_issue_url(item["issue_id"])
                title_short = item["title"][:70]
                d = item["delta"]
                d_str = f"+{d * 100:.0f}%" if d is not None else "—"
                attn_lines.append(
                    f"- [{item['platform']}] **{item['events']:,}** events ({d_str}) · "
                    f"[{title_short}]({url})"
                )
        if fatal_drops:
            attn_lines.append("")
            attn_lines.append(f"### 📉 下降 (<= -10% vs 上周同时段, {len(fatal_drops)} 项)")
            for item in sorted(fatal_drops, key=lambda x: x["delta"] or 0)[:5]:
                url = _frontend_issue_url(item["issue_id"])
                title_short = item["title"][:70]
                d = item["delta"]
                d_str = f"{d * 100:.0f}%" if d is not None else "—"
                attn_lines.append(
                    f"- [{item['platform']}] **{item['events']:,}** events ({d_str}) · "
                    f"[{title_short}]({url})"
                )

    attn_lines.append("")
    attn_lines.append("---")
    attn_lines.append("")

    # 早报追加昨日复盘（晚报为同日连续，跳过）
    retro_lines: List[str] = []
    if report_type == "morning":
        try:
            retro_md = await _retrospect_yesterday(target_date)
        except Exception:
            logger.exception("retrospect failed (non-fatal)")
            retro_md = None
        if retro_md:
            retro_lines = retro_md.split("\n")

    # 早晚报不再展示 PR 修复段——按用户要求，PR 内容只在 crashguard web 端查看
    # 早晚报只聚焦"出了什么事"，PR 状态查看走前端，避免群消息聒噪

    # 插入位置：title (line 0) + 数据窗口 (line 1) + 空行 (line 2) 后
    # 顺序：Σ 摘要 → 昨日复盘 → ✨ 关注点
    insert_at = 2
    lines[insert_at:insert_at] = [sigma, ""] + retro_lines + attn_lines

    payload["new_count"] = total_new
    payload["regression_count"] = total_surge
    payload["surge_count"] = total_surge
    payload["top_n"] = total_top
    # 关注点 issue id 集合（供 send_daily_report 触发 auto-analyze → auto-PR）
    # C 路线扩展：fatal news/surges + Top10 fatal + Top10 non_fatal + 全部新增（含 non_fatal）
    auto_pr_candidates.update(item["issue_id"] for item in (fatal_news + fatal_surges))
    payload["attention_issue_ids"] = sorted(auto_pr_candidates)

    text = "\n".join(lines).rstrip() + "\n"
    return text, payload


async def _retrospect_yesterday(target_date: date) -> Optional[str]:
    """
    早报开头追加"📋 昨日承诺今日兑现"段：
    解析昨日早/晚报 payload 里的 attention_issue_ids，今日查 issue.status：
    resolved_by_pr / ignored / wontfix → 已闭环；open / investigating → 仍在跟。
    返回 markdown 段；无昨日数据返回 None。
    """
    yesterday = target_date - timedelta(days=1)
    async with get_session() as session:
        rows = (await session.execute(
            select(CrashDailyReport).where(CrashDailyReport.report_date == yesterday)
        )).scalars().all()
        if not rows:
            return None
        # 合并昨日早晚报的 attention id（去重）
        attention_ids: set = set()
        for r in rows:
            try:
                payload = _json.loads(r.report_payload or "{}")
                for iid in (payload.get("attention_issue_ids") or []):
                    attention_ids.add(iid)
            except Exception:
                continue
        if not attention_ids:
            return None

        issues = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(list(attention_ids)))
        )).scalars().all()

    closed_states = {"resolved_by_pr", "ignored", "wontfix"}
    closed: List[CrashIssue] = []
    open_issues: List[CrashIssue] = []
    for issue in issues:
        st = (issue.status or "open").lower()
        if st in closed_states:
            closed.append(issue)
        else:
            open_issues.append(issue)

    total = len(attention_ids)
    closed_n = len(closed)
    open_n = len(open_issues)

    lines: List[str] = ["## 📋 昨日承诺 · 今日兑现"]
    lines.append("")
    lines.append(
        f"> 昨日关注点 **{total}** 项 → 已闭环 **{closed_n}** · 仍 open **{open_n}**"
    )
    if closed:
        lines.append("")
        lines.append("### ✅ 已闭环")
        for issue in closed[:5]:
            url = _frontend_issue_url(issue.datadog_issue_id)
            title_short = (issue.title or "")[:70]
            lines.append(f"- [{issue.status}] [{title_short}]({url})")
    if open_issues:
        lines.append("")
        lines.append("### ⏳ 仍在跟")
        for issue in open_issues[:5]:
            url = _frontend_issue_url(issue.datadog_issue_id)
            title_short = (issue.title or "")[:70]
            lines.append(f"- [{issue.status or 'open'}] [{title_short}]({url})")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


async def _recent_auto_prs(
    target_date: date,
    lookback_hours: int = 24,
) -> List[Dict[str, Any]]:
    """查最近 N 小时由 auto-flow 创建的 PR（approved_by='auto'），带 issue 标题。"""
    from datetime import timedelta
    from app.crashguard.models import CrashPullRequest, CrashIssue
    cutoff = datetime.utcnow() - timedelta(hours=max(1, int(lookback_hours)))
    out: List[Dict[str, Any]] = []
    async with get_session() as session:
        rows = (await session.execute(
            select(CrashPullRequest)
            .where(
                CrashPullRequest.created_at >= cutoff,
                CrashPullRequest.approved_by == "auto",
            )
            .order_by(CrashPullRequest.created_at.desc())
            .limit(20)
        )).scalars().all()
        if not rows:
            return out
        issue_ids = [r.datadog_issue_id for r in rows]
        issues = (await session.execute(
            select(CrashIssue.datadog_issue_id, CrashIssue.title)
            .where(CrashIssue.datadog_issue_id.in_(issue_ids))
        )).all()
        title_map = {row[0]: (row[1] or "") for row in issues}
    for r in rows:
        out.append({
            "datadog_issue_id": r.datadog_issue_id,
            "issue_title": title_map.get(r.datadog_issue_id, ""),
            "pr_url": r.pr_url or "",
            "pr_number": r.pr_number,
            "pr_status": r.pr_status or "draft",
            "repo": r.repo or "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


async def _auto_analyze_attention(issue_ids: List[str]) -> int:
    """对关注点 issue 中尚无 success root 分析的，**串行**跑 analyze_issue（含 auto-PR）。

    设计取舍：
    - 早报 7:00 触发，2 小时窗口足以跑完 ~20 个 issue（每个 30-90s + PR 30s）；
    - 串行避免：(a) 多个 Claude/Codex 子进程争抢；(b) 多 PR 同 repo git push race；
    - 单个 issue 失败不影响其他（捕获 exception 继续）。
    """
    if not issue_ids:
        return 0
    from app.crashguard.models import CrashAnalysis
    from app.crashguard.services.analyzer import analyze_issue

    async with get_session() as session:
        # 已有 success root 的 / 正在跑的 → 跳
        existing = (await session.execute(
            select(CrashAnalysis.datadog_issue_id, CrashAnalysis.status).where(
                CrashAnalysis.datadog_issue_id.in_(issue_ids),
                CrashAnalysis.followup_question == "",
                CrashAnalysis.status.in_(["success", "running", "pending"]),
            )
        )).all()
    skip_set = {row[0] for row in existing}

    pending = [iid for iid in issue_ids if iid not in skip_set]
    if not pending:
        logger.info("auto_analyze: all %d issues already analyzed/running, nothing to do", len(issue_ids))
        return 0
    logger.info("auto_analyze: %d pending issues, running serially...", len(pending))

    # 取去重窗口（小时）；防多个触发器（warmup + cron + UI）并发烧 token
    try:
        s = get_crashguard_settings()
        dedup_hours = int(getattr(s, "analysis_dedup_hours", 6) or 6)
    except Exception:
        dedup_hours = 6

    completed = 0
    for idx, iid in enumerate(pending, 1):
        # 二次去重：进入串行循环时（每个 iid 之前），可能在等待期间另一入口已经分析完。
        # 仅前置过滤（871-878）不够——那里一次性查 in_clause，循环中 DB 状态会变。
        if dedup_hours > 0:
            async with get_session() as session:
                recent = (await session.execute(
                    select(CrashAnalysis).where(
                        CrashAnalysis.datadog_issue_id == iid,
                        CrashAnalysis.status == "success",
                        CrashAnalysis.followup_question == "",
                        CrashAnalysis.created_at >= datetime.utcnow() - timedelta(hours=dedup_hours),
                    ).limit(1)
                )).scalar_one_or_none()
                if recent is not None:
                    logger.info(
                        "auto_analyze [%d/%d] dedup hit: %s skipped (success at %s, within %dh)",
                        idx, len(pending), iid, recent.created_at, dedup_hours,
                    )
                    continue
        try:
            logger.info("auto_analyze [%d/%d] analyzing %s", idx, len(pending), iid)
            await analyze_issue(iid)  # 串行：含 _maybe_auto_draft_pr hook
            completed += 1
        except Exception as exc:
            logger.warning("auto_analyze [%d/%d] %s failed: %s", idx, len(pending), iid, exc)
    logger.info("auto_analyze done: %d/%d issues completed", completed, len(pending))
    return completed


async def send_daily_report(
    report_type: str,
    target_date: date | None = None,
    top_n: int = 5,
    chat_id_override: str = "",
    email_override: str = "",
) -> Dict[str, Any]:
    """生成 → 发飞书 → 写 CrashDailyReport。

    推送目标优先级：email_override > chat_id_override > feishu_target_email > feishu_target_chat_id。
    """
    s = get_crashguard_settings()
    if target_date is None:
        target_date = date.today()

    if not s.feishu_enabled:
        try:
            from app.crashguard.services.audit import write_audit
            await write_audit(op="daily_report", target_id=report_type, success=False, error="feishu_disabled")
        except Exception:
            pass
        return {"ok": False, "sent": False, "skipped_reason": "feishu_disabled"}

    target_email = email_override or s.feishu_target_email
    chat_id = chat_id_override or s.feishu_target_chat_id
    if not target_email and not chat_id:
        try:
            from app.crashguard.services.audit import write_audit
            await write_audit(op="daily_report", target_id=report_type, success=False, error="no_target")
        except Exception:
            pass
        return {"ok": False, "sent": False, "skipped_reason": "no_target_chat_id_or_email"}

    # 多实例去重锁：抢先 INSERT 一行占位 (date, type)；
    # crash_daily_reports 上有 UniqueConstraint(report_date, report_type) → 第二个实例
    # 拿到 IntegrityError，直接返回 already_sent，不发飞书也不写 audit 失败。
    # 注：手动触发场景（chat_id_override 非空）跳过锁，允许重发。
    skip_lock = bool(chat_id_override or email_override)
    if not skip_lock:
        from sqlalchemy.exc import IntegrityError
        async with get_session() as session:
            try:
                placeholder = CrashDailyReport(
                    report_date=target_date,
                    report_type=report_type,
                    top_n=0,
                    new_count=0,
                    regression_count=0,
                    surge_count=0,
                    feishu_message_id="locking",
                    report_payload="{}",
                    created_at=datetime.utcnow(),
                )
                session.add(placeholder)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # 已有同 date+type 行 → 检查是不是另一实例正在 / 已经发过
                existing = (await session.execute(
                    select(CrashDailyReport).where(
                        CrashDailyReport.report_date == target_date,
                        CrashDailyReport.report_type == report_type,
                    )
                )).scalar_one_or_none()
                state = (existing.feishu_message_id or "") if existing else ""
                if state and state != "locking":
                    # 另一实例已成功发送
                    try:
                        from app.crashguard.services.audit import write_audit
                        await write_audit(
                            op="daily_report",
                            target_id=report_type,
                            success=True,
                            detail=f"skipped: another instance sent (state={state})",
                        )
                    except Exception:
                        pass
                    return {
                        "ok": True,
                        "sent": False,
                        "skipped_reason": "already_sent_by_other_instance",
                        "persisted_id": existing.id if existing else None,
                    }
                # state == "locking" 或空 — 检查是否为孤儿死锁（持有者已 crash）
                # 底层逻辑：locking 状态超过 LOCK_TTL_MIN 仍未变为真 message_id，
                # 说明上次持有者异常终止（curl 超时 / 进程崩溃），可安全接管。
                LOCK_TTL_MIN = 10
                is_stale = False
                if existing and existing.created_at:
                    age_sec = (datetime.utcnow() - existing.created_at).total_seconds()
                    if age_sec > LOCK_TTL_MIN * 60:
                        is_stale = True
                if is_stale:
                    # 接管：把孤儿行的 created_at 刷新，继续往下走
                    existing.created_at = datetime.utcnow()
                    await session.commit()
                    logger.warning(
                        "daily_report: taking over stale lock (age > %dmin) for %s/%s",
                        LOCK_TTL_MIN, target_date, report_type,
                    )
                    # 落 audit 记录接管事件
                    try:
                        from app.crashguard.services.audit import write_audit
                        await write_audit(
                            op="daily_report",
                            target_id=report_type,
                            success=True,
                            detail=f"took over stale lock (age>{LOCK_TTL_MIN}min)",
                        )
                    except Exception:
                        pass
                    # 不 return，跳出 IntegrityError 分支继续往下走 send 流程
                else:
                    try:
                        from app.crashguard.services.audit import write_audit
                        await write_audit(
                            op="daily_report",
                            target_id=report_type,
                            success=False,
                            error="lock_contended",
                            detail="another instance holds the lock",
                        )
                    except Exception:
                        pass
                    return {
                        "ok": False,
                        "sent": False,
                        "skipped_reason": "lock_contended",
                    }

    text, payload = await compose_report(report_type, target_date, top_n=top_n)

    # 关注点 issue 自动 AI 分析（真 fire-and-forget——不能 await，否则 20 个串行 AI 分析
    # 会阻塞飞书推送 20-30 分钟，导致早晚报 cron 触发后永远 timeout）
    auto_queued = 0
    try:
        attention_ids = payload.get("attention_issue_ids") or []
        if attention_ids:
            _spawn_bg(
                _auto_analyze_attention(attention_ids),
                name=f"daily-report-analyze-{report_type}-{target_date.isoformat()}",
            )
            auto_queued = len(attention_ids)
            logger.info(
                "daily_report: dispatched %d attention issues for AI analysis in background",
                len(attention_ids),
            )
    except Exception:
        logger.exception("auto-analyze attention dispatch failed (non-fatal)")

    # 优先用飞书 interactive card；失败回退到 text
    sent = False
    try:
        from app.services.feishu_cli import send_interactive_card, send_message
        from app.crashguard.services.feishu_card import build_daily_card
        card = build_daily_card(
            report_type=report_type,
            target_date=target_date.isoformat(),
            markdown=text,
            payload=payload,
            frontend_base_url=s.frontend_base_url or "http://localhost:3000",
        )
        if target_email:
            sent = await send_interactive_card(email=target_email, card=card)
            if not sent:
                logger.warning("interactive card (email) send failed, falling back to text")
                sent = await send_message(email=target_email, text=text)
        else:
            sent = await send_interactive_card(chat_id=chat_id, card=card)
            if not sent:
                logger.warning("interactive card (chat) send failed, falling back to text")
                sent = await send_message(chat_id=chat_id, text=text)
    except Exception:
        logger.exception("crashguard daily_report send failed")
        sent = False

    persisted_id = None
    async with get_session() as session:
        existing = (await session.execute(
            select(CrashDailyReport).where(
                CrashDailyReport.report_date == target_date,
                CrashDailyReport.report_type == report_type,
            )
        )).scalar_one_or_none()
        new_count = payload.get("new_count", 0)
        regression_count = payload.get("regression_count", 0)
        surge_count = payload.get("surge_count", 0)
        top_count = payload.get("top_n", 0)
        if existing is None:
            row = CrashDailyReport(
                report_date=target_date,
                report_type=report_type,
                top_n=top_count,
                new_count=new_count,
                regression_count=regression_count,
                surge_count=surge_count,
                feishu_message_id="sent" if sent else "",
                report_payload=_json.dumps(payload, ensure_ascii=False),
                created_at=datetime.utcnow(),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            persisted_id = row.id
        else:
            existing.top_n = top_count
            existing.new_count = new_count
            existing.regression_count = regression_count
            existing.surge_count = surge_count
            existing.feishu_message_id = "sent" if sent else existing.feishu_message_id
            existing.report_payload = _json.dumps(payload, ensure_ascii=False)
            await session.commit()
            persisted_id = existing.id

    # audit
    try:
        from app.crashguard.services.audit import write_audit
        await write_audit(
            op="daily_report",
            target_id=report_type,
            success=sent,
            detail={
                "report_date": target_date.isoformat(),
                "auto_analyze_queued": auto_queued,
                "platforms": payload.get("platforms"),
            },
            error="" if sent else "send_failed_or_no_chat",
        )
    except Exception:
        pass

    return {
        "ok": sent,
        "sent": sent,
        "skipped_reason": "" if sent else "send_failed_or_no_chat",
        "persisted_id": persisted_id,
        "preview": text[:2000],
        "auto_analyze_queued": auto_queued,
    }
