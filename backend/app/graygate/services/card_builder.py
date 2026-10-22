"""Graygate 飞书 interactive card 渲染（v2 schema）。

用户反馈第一版纯文本消息（`send_message(text=...)`）太丑——飞书 `msg_type: text`
不解析 `**加粗**` / `<details>` 折叠，整段消息挤成一坨。改用 interactive card，
照抄 `app/crashguard/services/feishu_card.py` 里已验证过的视觉语言：iOS/Android
双列布局、每列内"大盘 → 主要版本 → 🆕最新版本"三层、🟩/🟨/🟥 状态色点。

两层版本口径（2026-08-19：原有的第三层"🆕最新版本"自动判定被用户下线）：
  大盘（{version_pattern}）—— version_pattern 通配符全量聚合
  主要版本                 —— 优先用人工指定的 focus version（发新版本时运营
                              通过 API/前端手动设置，见 services/focus_version.py），
                              未设置时回落到 session 数最大的 build（自动判定）

按用户要求，新增崩溃/Top 卡顿固定排在全部版本数据之后（不是像第一版那样穿插在中间）。

本模块独立于 report_builder.py（不改它的 _SCOPES/_gate，避免动到已经过 57 个测试
锁定的两层口径逻辑），只复用双方共同依赖的底层取数函数。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.graygate.config import get_graygate_settings
from app.graygate.services.dashboard_query import (
    MetricSpec,
    get_dashboard_json,
    build_title_index,
    get_metric_scalar,
    load_metrics_config,
)
from app.graygate.services.version_resolver import (
    PlatformVersions, resolve_extra_version, resolve_versions, total_sessions,
)
from app.graygate.services.new_crashes import NewCrash, find_new_crashes
from app.graygate.services.top_issues import TopCrash, TopJank, find_top_crashes, find_top_jank
from app.graygate.services.focus_version import get_focus_version
from app.graygate.services.user_counts import (
    all_app_query, count_all_app_users, count_users, count_users_by_version,
)

_BJT = ZoneInfo("Asia/Shanghai")
_PLATFORMS = ("ios", "android")
logger = logging.getLogger("jarvis.graygate.card_builder")

_PLATFORM_LABEL = {"ios": "🍎 iOS", "android": "🤖 Android"}

_NOT_APPLICABLE = "—（不适用）"
_NO_DATA = "—（无数据）"
_QUERY_FAILED = "—（取数失败）"

CellValue = Any  # float | Tuple[float, float]


@dataclass
class GraygateReportCard:
    available: bool
    card: Dict[str, Any]  # available=False 时为 {}


@dataclass
class _Cell:
    value: Optional[CellValue]
    sentinel: Optional[str]


@dataclass
class TierSummary:
    """一个平台下一层口径（大盘 / 主要版本）的展示数据，已格式化。

    `columns_md` 是给飞书 column_set 拼好的整段 lark_md，没法再拆成表格；网页端
    完整日报（指标 × 平台口径 的表）和 Slack 精简版（只挑核心指标）都从这里取。
    """

    label: str                    # "大盘" / "主要版本"
    version: Optional[str]        # 大盘是 version_pattern；主要版本未判定出来时为 None
    sessions: Optional[int]
    manual: bool                  # 主要版本是否人工指定
    cells: Dict[str, str]         # metric key → 带色点的格式化值 / sentinel 文案
    # 2026-10-19：用户数 + 采样还原。`sessions` 仍是采样内会话数（样本量）；
    # 展示用 `display_sessions`（采样 <100% 时是 ≈估算全量）。
    users: Optional[int] = None   # distinct @usr.id（采样内）；取数失败为 None
    est_sessions: Optional[int] = None
    sample_rate: float = 100.0
    # 2026-10-20：占同平台全部 App 用户（Flutter 3.x + Native 4.x）的比例，单位 %；
    # 分子 / 分母任一取数失败为 None
    user_share: Optional[float] = None
    session_share: Optional[float] = None

    @property
    def share_str(self) -> Optional[str]:
        """`12.3% users / 10.1% sessions`；两个都没有时 None。"""
        if self.user_share is None and self.session_share is None:
            return None
        fmt = lambda v: f"{v:.1f}%" if v is not None else "—"
        return f"{fmt(self.user_share)} users / {fmt(self.session_share)} sessions"

    @property
    def sampled(self) -> bool:
        return self.sample_rate < 100

    @property
    def display_sessions(self) -> Optional[str]:
        n = self.est_sessions if self.est_sessions is not None else self.sessions
        if n is None:
            return None
        return f"≈{n:,}" if self.sampled else f"{n:,}"


@dataclass
class VersionRow:
    """网页完整日报「全版本明细」的一行：version_pattern 下每个 build。"""

    version: str
    sessions: int                 # 采样内
    est_sessions: int
    users: Optional[int]
    sample_rate: float


def _window_ms(day: date, until: Optional[time] = None) -> Tuple[int, int]:
    """`day` 的 BJT 00:00 → 24:00；给了 `until`（钟点）就是 00:00 → 当天这个钟点
    （「今日至今」报告，基线日也截到同一钟点，同时段对比）。"""
    start = datetime.combine(day, time.min, tzinfo=_BJT)
    end = (datetime.combine(day, until, tzinfo=_BJT) if until is not None
           else start + timedelta(days=1))
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _prev_workday(d: date) -> date:
    """跳过周末的"上一个工作日"：周一 → 上周五；其余 → 前一天。
    不处理法定节假日（超出本次范围，如需要后续可接节假日日历）。"""
    if d.weekday() == 0:  # Monday
        return d - timedelta(days=3)
    return d - timedelta(days=1)


def _metric_name(spec: MetricSpec) -> str:
    _OVERRIDES = {"jank": "App Jank Count per Session", "home_render": "Home File List Load Time"}
    override = _OVERRIDES.get(spec.key)
    if override:
        return override
    return spec.title or spec.title_p75 or spec.key


def _status_dot(target: Optional[Dict[str, Any]], value: Optional[float]) -> str:
    """target 缺失或取数失败 → 不打点，只显示数值。

    三档写法 {op, green, yellow} → 🟩/🟨/🟥；二档写法 {op, value} → 🟩/🟥。
    """
    if target is None or value is None:
        return ""
    op = target.get("op")

    def _passes(th: float) -> bool:
        return {">=": value >= th, ">": value > th, "<": value < th}.get(op, value <= th)

    if "green" in target:
        if _passes(target["green"]):
            return "🟩 "
        return "🟨 " if _passes(target["yellow"]) else "🟥 "
    return "🟩 " if _passes(target.get("value")) else "🟥 "


def _fmt_cell_value(spec: MetricSpec, cell: _Cell, platform: Optional[str] = None) -> str:
    if cell.sentinel:
        return cell.sentinel
    if isinstance(cell.value, tuple):
        p75, p90 = cell.value
        dot75 = _status_dot(spec.target_p75, p75)
        dot90 = _status_dot(spec.target_p90, p90)
        formatted = spec.cell_format.format(p75=p75, p90=p90)
        # cell_format 形如 "{p75:.1f}/{p90:.1f}"，分别给两段插色点不好拆，
        # 简化：整行前缀取"较差的那个"点（有一个不达标就标红）。
        dot = next((d for d in ("🟥 ", "🟨 ") if d in (dot75, dot90)), dot75 or dot90)
        return f"{dot}{formatted}"
    dot = _status_dot(spec.target_for(platform), cell.value)
    return f"{dot}{spec.cell_format.format(v=cell.value)}"


async def _resolve_cell(
    dashboard_json: dict,
    spec: MetricSpec,
    platform: str,
    version_value: Optional[str],
    sentinel: Optional[str],
    template_vars_base: Dict[str, str],
    from_ms: int,
    to_ms: int,
) -> _Cell:
    if sentinel:
        return _Cell(value=None, sentinel=sentinel)
    template_vars = {**template_vars_base, "service": f"plaud_{platform}", "version": version_value}
    # get_metric_scalar 在 widget 标题找不到/歧义时 raise ValueError（不是返回
    # None）——这是这个模块真正在生产环境跑的取数函数（scheduler.py 调的是
    # build_report_card，不是 report_builder.build_report 那份没人调用的旧实现）。
    # 2026-08-23 事故就是这里没 catch，一个 widget 标题漂移直接崩穿整份报告，
    # 连续两天飞书群什么都没收到。降级成跟"查询失败"同一档 sentinel，不再让
    # 一个指标的问题拖垮其它几十个指标。
    try:
        if spec.title:
            v = await get_metric_scalar(dashboard_json, spec.title, template_vars, from_ms, to_ms)
            if v is None:
                return _Cell(value=None, sentinel=_QUERY_FAILED)
            return _Cell(value=v * spec.scale, sentinel=None)
        p75 = await get_metric_scalar(dashboard_json, spec.title_p75, template_vars, from_ms, to_ms)
        p90 = await get_metric_scalar(dashboard_json, spec.title_p90, template_vars, from_ms, to_ms)
        if p75 is None or p90 is None:
            return _Cell(value=None, sentinel=_QUERY_FAILED)
        return _Cell(value=(p75 * spec.scale, p90 * spec.scale), sentinel=None)
    except ValueError as e:
        logger.warning("graygate metric %r widget lookup failed, degrading to %r: %s", spec.key, _QUERY_FAILED, e)
        return _Cell(value=None, sentinel=_QUERY_FAILED)


@dataclass
class _TierGate:
    version_value: Optional[str]
    sentinel: Optional[str]  # None → 应该发起查询


def _gate_tier(
    metrics: List[MetricSpec],
    platform: str,
    version_value: Optional[str],
    sample_proxy: int,
) -> Dict[str, _TierGate]:
    """判定每个指标该不该查（不适用平台/无版本数据/0 会话），只判一次，
    今日窗口和基线窗口共用同一份判定结果（避免基线样本量单独判定导致两天
    走不同分支、口径不一致）。"""
    gates: Dict[str, _TierGate] = {}
    for spec in metrics:
        if spec.not_applicable_platform == platform:
            gates[spec.key] = _TierGate(None, _NOT_APPLICABLE)
        elif version_value is None:
            gates[spec.key] = _TierGate(None, _NO_DATA)
        elif sample_proxy <= 0:
            # 2026-10-20：灰度报表取消 min_sessions=50 样本地板——灰度包放量初期
            # 会话很少，但哪怕只有 1 个会话也要按时出数；只有 0 会话才标无数据。
            # （crashguard/coreguard 各自的样本门槛不受影响。）
            gates[spec.key] = _TierGate(version_value, _NO_DATA)
        else:
            gates[spec.key] = _TierGate(version_value, None)
    return gates


async def _resolve_tier_for_window(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    gates: Dict[str, _TierGate],
    template_vars_base: Dict[str, str],
    from_ms: int,
    to_ms: int,
) -> Dict[str, _Cell]:
    out: Dict[str, _Cell] = {}
    for spec in metrics:
        g = gates[spec.key]
        if g.sentinel:
            out[spec.key] = _Cell(value=None, sentinel=g.sentinel)
            continue
        out[spec.key] = await _resolve_cell(
            dashboard_json, spec, platform, g.version_value, None,
            template_vars_base, from_ms, to_ms,
        )
    return out


def _tier_md(
    title_md: str,
    metrics: List[MetricSpec],
    cells: Optional[Dict[str, _Cell]],
    note: str = "",
    active_users: Optional[int] = None,
    platform: Optional[str] = None,
) -> List[str]:
    # active_users：session 数代理（version_resolver 里本来就为了选主力版本/样本
    # 地板算过的数字，这里加一行明确展示出来，不是新起一次 Datadog 查询——2026-08-23
    # 看板上没有现成的 DAU/活跃用户 widget，distinct 用户数需要额外一次
    # cardinality(@usr.id) 调用且需要先确认字段填充率，先用现有 session 数顶上）。
    active_line = f"· 活跃用户数（session 代理）：{_fmt_n(active_users)}" if active_users is not None else ""
    if cells is None:
        lines = [title_md, note] if note else [title_md]
        if active_line:
            lines.append(active_line)
        return lines
    lines = [title_md]
    if note:
        lines.append(note)
    if active_line:
        lines.append(active_line)
    for spec in metrics:
        lines.append(f"· {_metric_name(spec)}：{_fmt_cell_value(spec, cells[spec.key], platform)}")
    return lines


def _fmt_n(n: int) -> str:
    return f"{n:,}"


def _volume_str(sessions: str, users: Optional[int]) -> str:
    """`1,670 sessions · 214 users`；用户数取数失败时只给会话数。"""
    return f"{sessions} sessions" + (f" · {_fmt_n(users)} users" if users is not None else "")


# ---------------------------------------------------------------------------
# 恶化摘要段（连续两个工作日同向恶化才算）—— 只看核心指标，不在每行加 delta
# ---------------------------------------------------------------------------
#
# 2026-08-19 用户反馈：第一版单日对比把 P90/比率类指标的日常波动也标红了
# （现场核实：iOS 4.0.301-1038 当天流量其实是涨的——900→1629 sessions，
# 不是"样本崩了导致失真"，但 P90/Hang Rate/ANR 这类统计量本来就比均值类
# 指标更容易被少数极端样本带偏，尤其灰度早期）。用户裁决两条：
#   1. 只有"核心指标"参与恶化判定——崩溃(crash_free/android_anr/hang_rate)、
#      卡顿(jank)、内存(memory_usage)、Refresh Rate、冷启动(cold_startup_p90)。
#      页面渲染耗时类(fps/home_render/detail_render_p90/summary_render_p90)
#      不参与——仍然正常显示数值，只是不会被拿来判定"恶化"。
#   2. 连续两个工作日同向恶化才算，不再是单日对比就标红（照抄 coreguard
#      现有的 N=2 连续 breach 防抖设计思路）。

_CORE_WORSEN_KEYS = {
    "crash_free", "android_anr", "hang_rate",   # 崩溃
    "jank",                                      # 卡顿
    "memory_usage",                              # 内存
    "refresh_rate",                              # Refresh Rate
    "cold_startup_p90",                          # 冷启动
}


def _widget_directionality(
    dashboard_json: dict,
    title_index: Dict[str, Optional[int]],
    spec: MetricSpec,
) -> Optional[str]:
    title = spec.title or spec.title_p75
    if not title:
        return None
    idx = title_index.get(title)
    if idx is None:
        return None
    widgets = dashboard_json.get("widgets", [])
    if idx >= len(widgets):
        return None
    requests = (widgets[idx].get("definition") or {}).get("requests") or []
    if not requests:
        return None
    return (requests[0].get("comparison") or {}).get("directionality")


def _is_worse(directionality: Optional[str], delta: float) -> bool:
    if directionality == "increase_better":
        return delta < 0
    if directionality == "decrease_better":
        return delta > 0
    return False


def _breach_threshold(spec: MetricSpec, delta: float, baseline: float) -> bool:
    """照抄 report_builder.py 已验证过的默认阈值：% 结尾指标用 0.5pp 绝对变化，
    其余用 20% 相对变化。"""
    if spec.cell_format.endswith("%"):
        return abs(delta) >= 0.5
    if baseline == 0:
        return False
    return abs(delta / baseline) >= 0.20


def _format_delta(spec: MetricSpec, delta: float, baseline: float) -> str:
    if spec.cell_format.endswith("%"):
        return f"{delta:+.2f}pp"
    if baseline == 0:
        return f"{delta:+.2f}"
    return f"{delta / baseline * 100:+.1f}%"


def _breach(spec: MetricSpec, directionality: Optional[str], cur: float, base: float) -> bool:
    delta = cur - base
    return _breach_threshold(spec, delta, base) and _is_worse(directionality, delta)


@dataclass
class _WorsenCandidate:
    platform: str
    tier_label: str
    spec: MetricSpec
    today: _Cell   # 今日
    d1: _Cell      # 上一个工作日（第一次对比的基线）
    d2: _Cell      # 上上一个工作日（第二次对比的基线，用于"连续两天"判定）


@dataclass
class WorsenItem:
    """恶化条目的结构化版本（Slack 英文版用；飞书/网页端仍用拼好的 worsen_lines）。"""

    platform: str
    tier_label: str     # "大盘" / "主要版本"
    key: str            # metric key
    value: str          # 今日值（已按 cell_format 格式化）
    arrow: str          # ▲ / ▼
    delta: str          # +37.7% / +0.12pp


def _build_worsen_lines(
    candidates: List[_WorsenCandidate],
    directionality_by_key: Dict[str, Optional[str]],
    items_out: Optional[List[WorsenItem]] = None,
) -> List[str]:
    """连续两个工作日同向恶化才标记：今日 vs 上一工作日 要恶化，且上一工作日
    vs 上上工作日 也要恶化（同一子值维度），单日波动不会触发。"""
    lines: List[str] = []
    for c in candidates:
        if c.today.value is None or c.d1.value is None or c.d2.value is None:
            continue
        directionality = directionality_by_key.get(c.spec.key)
        if isinstance(c.today.value, tuple):
            sub_indices: List[Optional[int]] = list(range(len(c.today.value)))
        else:
            sub_indices = [None]

        worst_delta = None
        worst_baseline = None
        for idx in sub_indices:
            if idx is None:
                cur, d1v, d2v = c.today.value, c.d1.value, c.d2.value
            else:
                cur, d1v, d2v = c.today.value[idx], c.d1.value[idx], c.d2.value[idx]
            if _breach(c.spec, directionality, cur, d1v) and _breach(c.spec, directionality, d1v, d2v):
                delta = cur - d1v
                if worst_delta is None or abs(delta) > abs(worst_delta):
                    worst_delta, worst_baseline = delta, d1v
        if worst_delta is None:
            continue
        arrow = "▲" if worst_delta > 0 else "▼"
        if isinstance(c.today.value, tuple):
            cur_str = c.spec.cell_format.format(p75=c.today.value[0], p90=c.today.value[1])
        else:
            cur_str = c.spec.cell_format.format(v=c.today.value)
        delta_str = _format_delta(c.spec, worst_delta, worst_baseline)
        lines.append(
            f"- {_PLATFORM_LABEL[c.platform]} [{c.tier_label}] {_metric_name(c.spec)} "
            f"{cur_str} {arrow} {delta_str}（连续2个工作日）"
        )
        if items_out is not None:
            items_out.append(WorsenItem(c.platform, c.tier_label, c.spec.key, cur_str, arrow, delta_str))
    return lines


async def _fetch_tier_history(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    version_value: Optional[str],
    sample_proxy: int,
    template_vars_base: Dict[str, str],
    today_ms: Tuple[int, int],
    core_metrics: List[MetricSpec],
    d1_ms: Tuple[int, int],
    d2_ms: Tuple[int, int],
) -> Tuple[Dict[str, _Cell], Dict[str, _Cell], Dict[str, _Cell]]:
    """今日查全部指标（用于展示）；D1（上一个工作日）/D2（上上个工作日）只查
    核心指标（用于恶化的连续两天判定，非核心指标不需要历史数据）。三次查询
    用同一个 version_value——"同一个包比较三天"，不是各自重新挑主力包
    （对齐 report_builder.py 已验证过的设计：DoD 对比要 apples-to-apples）。"""
    gates = _gate_tier(metrics, platform, version_value, sample_proxy)
    today_cells = await _resolve_tier_for_window(
        dashboard_json, metrics, platform, gates, template_vars_base, *today_ms,
    )
    core_gates = _gate_tier(core_metrics, platform, version_value, sample_proxy)
    d1_cells = await _resolve_tier_for_window(
        dashboard_json, core_metrics, platform, core_gates, template_vars_base, *d1_ms,
    )
    d2_cells = await _resolve_tier_for_window(
        dashboard_json, core_metrics, platform, core_gates, template_vars_base, *d2_ms,
    )
    return today_cells, d1_cells, d2_cells


async def _build_platform_column(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    pv: PlatformVersions,
    version_pattern: str,
    template_vars_base: Dict[str, str],
    today_ms: Tuple[int, int],
    d1_ms: Tuple[int, int],
    d2_ms: Tuple[int, int],
    worsen_candidates: List[_WorsenCandidate],
    tiers_out: Optional[List[TierSummary]] = None,
) -> str:
    core_metrics = [m for m in metrics if m.key in _CORE_WORSEN_KEYS]
    lines: List[str] = [f"**{_PLATFORM_LABEL[platform]}**", ""]

    def _collect(tier_label: str, today: Dict[str, _Cell], d1: Dict[str, _Cell], d2: Dict[str, _Cell]) -> None:
        for spec in core_metrics:
            worsen_candidates.append(_WorsenCandidate(
                platform, tier_label, spec, today[spec.key], d1[spec.key], d2[spec.key],
            ))

    # 大盘
    market_today, market_d1, market_d2 = await _fetch_tier_history(
        dashboard_json, metrics, platform, version_pattern, pv.total_events,
        template_vars_base, today_ms, core_metrics, d1_ms, d2_ms,
    )
    market = TierSummary(
        "大盘", version_pattern, pv.total_events, False,
        {m.key: _fmt_cell_value(m, market_today[m.key], platform) for m in metrics},
        users=await count_users(platform, version_pattern, *today_ms) if pv.total_events else None,
        est_sessions=pv.est_sessions(),
        sample_rate=pv.sample_rate(),
    )
    lines += _tier_md(
        f"__大盘（{version_pattern}）__（{_volume_str(market.display_sessions, market.users)}）",
        metrics, market_today, platform=platform,
    )
    lines.append("")
    _collect("大盘", market_today, market_d1, market_d2)
    if tiers_out is not None:
        tiers_out.append(market)
    base_users, base_sessions = await _share_base(platform, today_ms)
    _fill_share(market, base_users, base_sessions)

    # 主要版本：优先用人工指定的 focus version（发新版本时运营手动设置，见
    # services/focus_version.py）；未设置时回落到 session 数自动判定的 top_version。
    override_version = await get_focus_version(platform)
    if override_version:
        primary_version = override_version
        # 指定的 build 可能不在 version_pattern 内（枚举里没有）→ 单独补查会话数，
        # 否则会取成 0、全部指标被判「样本不足」（2026-10-20 4.0.1-1268 全 0 事故）。
        await resolve_extra_version(pv, override_version, *today_ms)
        primary_sessions = pv.sessions(override_version)
        primary_suffix = "（人工指定）"
    else:
        primary_version = pv.top_version
        primary_sessions = pv.top_version_events
        primary_suffix = ""

    if primary_version:
        primary_today, primary_d1, primary_d2 = await _fetch_tier_history(
            dashboard_json, metrics, platform, primary_version, primary_sessions,
            template_vars_base, today_ms, core_metrics, d1_ms, d2_ms,
        )
        primary = TierSummary(
            "主要版本", primary_version, primary_sessions, bool(override_version),
            {m.key: _fmt_cell_value(m, primary_today[m.key], platform) for m in metrics},
            users=await count_users(platform, primary_version, *today_ms) if primary_sessions else None,
            est_sessions=pv.est_sessions(primary_version),
            sample_rate=pv.sample_rate(primary_version),
        )
        lines += _tier_md(
            f"__主要版本__ `{primary_version}`（{_volume_str(primary.display_sessions, primary.users)}）"
            f"{primary_suffix}",
            metrics, primary_today, platform=platform,
        )
        _collect("主要版本", primary_today, primary_d1, primary_d2)
        _fill_share(primary, base_users, base_sessions)
        if tiers_out is not None:
            tiers_out.append(primary)
    else:
        lines += _tier_md("__主要版本__", metrics, None, _NO_DATA)
        if tiers_out is not None:
            tiers_out.append(TierSummary(
                "主要版本", None, None, False, {m.key: _NO_DATA for m in metrics},
            ))

    rate = pv.sample_rate()
    if rate < 100:
        lines += ["", f"_RUM 采样 {rate:g}%：会话数已按采样率还原（≈），用户数为采样内去重，偏低_"]
    return "\n".join(lines)


def _pct(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    if part is None or not whole:
        return None
    return part / whole * 100.0


async def _share_base(platform: str, today_ms: Tuple[int, int]) -> Tuple[Optional[int], Optional[int]]:
    """占比分母：同平台全部 App（Flutter 3.x + Native 4.x）的（去重用户数, 估算会话数）。"""
    return (await count_all_app_users(platform, *today_ms),
            await total_sessions(all_app_query(platform), *today_ms))


def _fill_share(t: TierSummary, base_users: Optional[int], base_sessions: Optional[int]) -> None:
    t.user_share = _pct(t.users, base_users)
    sessions = t.est_sessions if t.est_sessions is not None else t.sessions
    t.session_share = _pct(sessions, base_sessions)


async def _version_rows(pv: PlatformVersions, version_pattern: str,
                        today_ms: Tuple[int, int]) -> List[VersionRow]:
    """version_pattern 下每个 build 的会话 / 用户 / 采样率（网页完整日报全版本明细）。"""
    if not pv.versions:
        return []
    users = await count_users_by_version(pv.platform, version_pattern, *today_ms)
    rows = [
        VersionRow(v, n, pv.est_sessions(v), users.get(v) if users else None, pv.sample_rate(v))
        for v, n in pv.versions
    ]
    rows.sort(key=lambda r: (-r.est_sessions, r.version))
    return rows


def _div(content: str) -> Dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _link(text: str, url: str) -> str:
    """把整段文字（不只是一个箭头符号）包成一个 markdown 链接，扩大可点击范围。"""
    return f"[{text} →]({url})"


def _build_new_crash_md(crashes: List[NewCrash]) -> Optional[str]:
    if not crashes:
        return None
    lines = ["**🆕 新增崩溃堆栈**", ""]
    for c in crashes:
        lines.append(
            f"- {c.platform.upper()} · `{c.version}` · **{c.events_count}** events · "
            f"{_link(c.title, c.datadog_url)}"
        )
    return "\n".join(lines)


def _build_top_crash_md(crashes: List[TopCrash]) -> Optional[str]:
    if not crashes:
        return None
    lines = ["**🔥 Top 5 崩溃（按 events，不限是否新增）**", ""]
    for i, c in enumerate(crashes, 1):
        lines.append(
            f"{i}. {c.platform.upper()} · **{_fmt_n(c.events_count)}** events · "
            f"{_link(c.title, c.datadog_url)}"
        )
    return "\n".join(lines)


def _build_top_jank_md(janks: List[TopJank]) -> Optional[str]:
    if not janks:
        return None
    lines = ["**🟠 Top 5 卡顿（按 events，不限是否新增）**", ""]
    for i, j in enumerate(janks, 1):
        lines.append(
            f"{i}. {j.platform.upper()} · **{_fmt_n(j.events_count)}** events · "
            f"{_link(j.label, j.datadog_url)}"
        )
    return "\n".join(lines)


@dataclass
class GraygateReportData:
    """一次日报的**全部内容**，渲染无关。

    拆出这层是为了**不让 Slack 路径把 Datadog 再查一遍** —— 上面那堆
    `_fetch_tier_history` / `find_new_crashes` / `find_top_crashes` 是这个模块
    最贵的部分（每个平台每个口径各三个时间窗），复制一份给 Slack 用既慢又会
    立刻漂移。

    ⚠️ 这**不是**设计文档里否决的那个「中立卡片 IR」。这里装的是**数据**
    （已经拼好的 markdown 片段 + 严重度），不是"标题/分栏/折叠"这种结构原语；
    两个 provider 各自决定这些片段怎么排版。
    """

    target_date: date
    d1_day: date
    version_pattern: str
    worsen_lines: List[str]
    columns_md: List[str]                 # [ios, android]
    new_crash_md: Optional[str]
    top_crash_md: Optional[str]
    top_jank_md: Optional[str]
    # 以下是给网页端完整日报 / Slack 精简版的结构化数据，飞书卡片不用。
    # 带默认值：老代码/测试只传上面那些字段时照常工作（渲染端会回落到 columns_md）。
    metric_rows: List[Tuple[str, str, bool]] = field(default_factory=list)  # (key, 展示名, 是否核心指标)
    tiers: Dict[str, List[TierSummary]] = field(default_factory=dict)       # platform → [大盘, 主要版本]
    new_crash_count: int = 0
    worsen_items: List[WorsenItem] = field(default_factory=list)
    version_rows: Dict[str, List[VersionRow]] = field(default_factory=dict)  # platform → 全版本明细
    until: Optional[time] = None  # 非空 = 「今日至今」高频报告，窗口截到这个 BJT 钟点

    @property
    def window_end_label(self) -> str:
        return self.until.strftime("%H:%M") if self.until else "24:00"

    @property
    def is_red(self) -> bool:
        """红色（有恶化或有新增崩溃）vs 绿色。两个渠道共用同一判据。"""
        return bool(self.worsen_lines or self.new_crash_md)

    @property
    def banner_md(self) -> str:
        return (
            f"📊 窗口 {self.target_date.strftime('%m-%d')} 00:00~{self.window_end_label} BJT · "
            f"基线 {self.d1_day.strftime('%m-%d')}（上一个工作日"
            f"{'同时段' if self.until else ''}）· "
            f"大盘版本模式 `{self.version_pattern}`"
        )

    @property
    def title(self) -> str:
        if self.until:
            return (f"🆕 [{self.version_pattern} 灰度] 今日至今 · {self.target_date.isoformat()} "
                    f"00:00–{self.window_end_label}")
        return f"🆕 [{self.version_pattern} 灰度] 每日指标 · {self.target_date.isoformat()}"


async def collect_report_data(target_date: date,
                              until: Optional[time] = None) -> Optional[GraygateReportData]:
    """跑完取数并拼好所有 markdown 片段。两平台版本枚举都空时返回 `None`。

    `target_date` 是 BJT 日历日（日报代表"昨日"）。`until`（BJT 钟点）非空时是
    「今日至今」高频报告：窗口 00:00 → until，两个基线工作日同样截到 until。
    """
    settings = get_graygate_settings()
    today_ms = _window_ms(target_date, until)
    d1_day = _prev_workday(target_date)       # 上一个工作日
    d2_day = _prev_workday(d1_day)             # 上上个工作日（连续两天判定用）
    d1_ms = _window_ms(d1_day, until)
    d2_ms = _window_ms(d2_day, until)

    versions = await resolve_versions(*today_ms)
    ios_v, android_v = versions["ios"], versions["android"]
    if ios_v.top_version is None and android_v.top_version is None:
        return None

    metrics_config = load_metrics_config()
    dashboard_json = await get_dashboard_json(settings.dashboard_id)
    title_index = build_title_index(dashboard_json.get("widgets", []))
    directionality_by_key = {
        spec.key: _widget_directionality(dashboard_json, title_index, spec)
        for spec in metrics_config.metrics
    }

    worsen_candidates: List[_WorsenCandidate] = []
    columns_md: List[str] = []
    tiers: Dict[str, List[TierSummary]] = {}
    for platform, pv in (("ios", ios_v), ("android", android_v)):
        tiers[platform] = []
        col_md = await _build_platform_column(
            dashboard_json, metrics_config.metrics, platform, pv,
            settings.version_pattern,
            metrics_config.template_variables, today_ms, d1_ms, d2_ms,
            worsen_candidates, tiers[platform],
        )
        columns_md.append(col_md)
    version_rows = {
        platform: await _version_rows(pv, settings.version_pattern, today_ms)
        for platform, pv in (("ios", ios_v), ("android", android_v))
    }

    worsen_items: List[WorsenItem] = []
    worsen_lines = _build_worsen_lines(worsen_candidates, directionality_by_key, worsen_items)

    new_crashes = await find_new_crashes(target_date)
    top_crashes = await find_top_crashes(target_date)
    top_jank = await find_top_jank(target_date)

    return GraygateReportData(
        target_date=target_date,
        d1_day=d1_day,
        version_pattern=settings.version_pattern,
        worsen_lines=worsen_lines,
        columns_md=columns_md,
        new_crash_md=_build_new_crash_md(new_crashes),
        top_crash_md=_build_top_crash_md(top_crashes),
        top_jank_md=_build_top_jank_md(top_jank),
        metric_rows=[(m.key, _metric_name(m), m.key in _CORE_WORSEN_KEYS) for m in metrics_config.metrics],
        tiers=tiers,
        new_crash_count=len(new_crashes),
        worsen_items=worsen_items,
        version_rows=version_rows,
        until=until,
    )


def assemble_feishu_card(data: GraygateReportData) -> Dict[str, Any]:
    """`GraygateReportData` → 飞书 interactive card（v2 schema）。

    结构（自上而下）：header → 🔴 恶化摘要（核心指标连续两个工作日同向恶化才出，
    有才出）→ iOS/Android 双列版本数据（大盘/主要版本）→ 🆕 新增崩溃堆栈
    （有才出）→ 🔥 Top5 崩溃 + 🟠 Top5 卡顿（有才出，不看是否新增，按 events 量）。

    ⚠️ 这个函数的输出必须跟拆分前**逐字节一致** —— 迁移期飞书仍是生产渠道，
    `tests/graygate/test_card_builder.py` 钉住了这件事。
    """
    elements: List[Dict[str, Any]] = [_div(data.banner_md)]

    if data.worsen_lines:
        elements.append({"tag": "hr"})
        elements.append(_div(
            "**🔴 恶化（核心指标，连续 2 个工作日同向恶化）**\n\n" + "\n".join(data.worsen_lines)
        ))

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
             "elements": [_div(data.columns_md[0])]},
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
             "elements": [_div(data.columns_md[1])]},
        ],
    })

    if data.new_crash_md:
        elements.append({"tag": "hr"})
        elements.append(_div(data.new_crash_md))

    if data.top_crash_md or data.top_jank_md:
        elements.append({"tag": "hr"})
        if data.top_crash_md:
            elements.append(_div(data.top_crash_md))
        if data.top_jank_md:
            elements.append(_div(data.top_jank_md))

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "red" if data.is_red else "turquoise",
            "title": {"tag": "plain_text", "content": data.title},
        },
        "body": {"elements": elements},
    }


async def build_report_card(target_date: date) -> GraygateReportCard:
    """组装 4.0.3 灰度日报 interactive card（飞书）。

    取数与渲染已经拆开：取数在 `collect_report_data()`，Slack 侧走
    `services/slack_report.py::build_report_message()` 消费同一份数据。
    """
    data = await collect_report_data(target_date)
    if data is None:
        return GraygateReportCard(available=False, card={})
    return GraygateReportCard(available=True, card=assemble_feishu_card(data))
