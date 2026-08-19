"""Graygate 飞书 interactive card 渲染（v2 schema）。

用户反馈第一版纯文本消息（`send_message(text=...)`）太丑——飞书 `msg_type: text`
不解析 `**加粗**` / `<details>` 折叠，整段消息挤成一坨。改用 interactive card，
照抄 `app/crashguard/services/feishu_card.py` 里已验证过的视觉语言：iOS/Android
双列布局、每列内"大盘 → 主要版本 → 🆕最新版本"三层、🟩/🟥 状态色点。

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

from dataclasses import dataclass
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
from app.graygate.services.version_resolver import PlatformVersions, resolve_versions
from app.graygate.services.new_crashes import NewCrash, find_new_crashes
from app.graygate.services.top_issues import TopCrash, TopJank, find_top_crashes, find_top_jank
from app.graygate.services.focus_version import get_focus_version

_BJT = ZoneInfo("Asia/Shanghai")
_PLATFORMS = ("ios", "android")
_PLATFORM_LABEL = {"ios": "🍎 iOS", "android": "🤖 Android"}

_NOT_APPLICABLE = "—（不适用）"
_NO_DATA = "—（无数据）"
_INSUFFICIENT_SAMPLE = "—（样本不足）"
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


def _window_ms(day: date) -> Tuple[int, int]:
    start = datetime.combine(day, time.min, tzinfo=_BJT)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _prev_workday(d: date) -> date:
    """跳过周末的"上一个工作日"：周一 → 上周五；其余 → 前一天。
    不处理法定节假日（超出本次范围，如需要后续可接节假日日历）。"""
    if d.weekday() == 0:  # Monday
        return d - timedelta(days=3)
    return d - timedelta(days=1)


def _metric_name(spec: MetricSpec) -> str:
    _OVERRIDES = {"jank": "APP单次使用的卡顿次数", "home_render": "首页文件列表加载耗时"}
    override = _OVERRIDES.get(spec.key)
    if override:
        return override
    return spec.title or spec.title_p75 or spec.key


def _status_dot(target: Optional[Dict[str, Any]], value: Optional[float]) -> str:
    """target 缺失或取数失败 → 不打点，只显示数值。"""
    if target is None or value is None:
        return ""
    op = target.get("op")
    th = target.get("value")
    ok = (value >= th) if op == ">=" else (value <= th)
    return "🟩 " if ok else "🟥 "


def _fmt_cell_value(spec: MetricSpec, cell: _Cell) -> str:
    if cell.sentinel:
        return cell.sentinel
    if isinstance(cell.value, tuple):
        p75, p90 = cell.value
        dot75 = _status_dot(spec.target_p75, p75)
        dot90 = _status_dot(spec.target_p90, p90)
        formatted = spec.cell_format.format(p75=p75, p90=p90)
        # cell_format 形如 "{p75:.1f}/{p90:.1f}"，分别给两段插色点不好拆，
        # 简化：整行前缀取"较差的那个"点（有一个不达标就标红）。
        dot = "🟥 " if ("🟥" in dot75 or "🟥" in dot90) else (dot75 or dot90)
        return f"{dot}{formatted}"
    dot = _status_dot(spec.target, cell.value)
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


@dataclass
class _TierGate:
    version_value: Optional[str]
    sentinel: Optional[str]  # None → 应该发起查询


def _gate_tier(
    metrics: List[MetricSpec],
    platform: str,
    version_value: Optional[str],
    sample_proxy: int,
    min_sessions: int,
) -> Dict[str, _TierGate]:
    """判定每个指标该不该查（不适用平台/无版本数据/样本不足），只判一次，
    今日窗口和基线窗口共用同一份判定结果（避免基线样本量单独判定导致两天
    走不同分支、口径不一致）。"""
    gates: Dict[str, _TierGate] = {}
    for spec in metrics:
        if spec.not_applicable_platform == platform:
            gates[spec.key] = _TierGate(None, _NOT_APPLICABLE)
        elif version_value is None:
            gates[spec.key] = _TierGate(None, _NO_DATA)
        elif sample_proxy < min_sessions:
            gates[spec.key] = _TierGate(version_value, _INSUFFICIENT_SAMPLE)
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


def _tier_md(title_md: str, metrics: List[MetricSpec], cells: Optional[Dict[str, _Cell]], note: str = "") -> List[str]:
    if cells is None:
        return [title_md, note] if note else [title_md]
    lines = [title_md]
    if note:
        lines.append(note)
    for spec in metrics:
        lines.append(f"· {_metric_name(spec)}：{_fmt_cell_value(spec, cells[spec.key])}")
    return lines


def _fmt_n(n: int) -> str:
    return f"{n:,}"


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


def _build_worsen_lines(
    candidates: List[_WorsenCandidate],
    directionality_by_key: Dict[str, Optional[str]],
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
        lines.append(
            f"- {_PLATFORM_LABEL[c.platform]} [{c.tier_label}] {_metric_name(c.spec)} "
            f"{cur_str} {arrow} {_format_delta(c.spec, worst_delta, worst_baseline)}（连续2个工作日）"
        )
    return lines


async def _fetch_tier_history(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    version_value: Optional[str],
    sample_proxy: int,
    min_sessions: int,
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
    gates = _gate_tier(metrics, platform, version_value, sample_proxy, min_sessions)
    today_cells = await _resolve_tier_for_window(
        dashboard_json, metrics, platform, gates, template_vars_base, *today_ms,
    )
    core_gates = _gate_tier(core_metrics, platform, version_value, sample_proxy, min_sessions)
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
    min_sessions: int,
    template_vars_base: Dict[str, str],
    today_ms: Tuple[int, int],
    d1_ms: Tuple[int, int],
    d2_ms: Tuple[int, int],
    worsen_candidates: List[_WorsenCandidate],
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
        min_sessions, template_vars_base, today_ms, core_metrics, d1_ms, d2_ms,
    )
    lines += _tier_md(f"__大盘（{version_pattern}）__", metrics, market_today)
    lines.append("")
    _collect("大盘", market_today, market_d1, market_d2)

    # 主要版本：优先用人工指定的 focus version（发新版本时运营手动设置，见
    # services/focus_version.py）；未设置时回落到 session 数自动判定的 top_version。
    override_version = await get_focus_version(platform)
    if override_version:
        primary_version = override_version
        primary_sessions = dict(pv.versions).get(override_version, 0)
        primary_suffix = "（人工指定）"
    else:
        primary_version = pv.top_version
        primary_sessions = pv.top_version_events
        primary_suffix = ""

    if primary_version:
        primary_today, primary_d1, primary_d2 = await _fetch_tier_history(
            dashboard_json, metrics, platform, primary_version, primary_sessions,
            min_sessions, template_vars_base, today_ms, core_metrics, d1_ms, d2_ms,
        )
        lines += _tier_md(
            f"__主要版本__ `{primary_version}`（{_fmt_n(primary_sessions)} sessions）{primary_suffix}",
            metrics, primary_today,
        )
        _collect("主要版本", primary_today, primary_d1, primary_d2)
    else:
        lines += _tier_md("__主要版本__", metrics, None, _NO_DATA)

    return "\n".join(lines)


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


async def build_report_card(target_date: date) -> GraygateReportCard:
    """组装 4.0.3 灰度日报 interactive card。target_date 是 BJT 日历日（代表"昨日"）。

    结构（自上而下）：header → 🔴 恶化摘要（核心指标连续两个工作日同向恶化才出，
    有才出）→ iOS/Android 双列版本数据（大盘/主要版本/🆕最新版本）→
    🆕 新增崩溃堆栈（有才出）→ 🔥 Top5 崩溃 + 🟠 Top5 卡顿（有才出，不看是否
    新增，按 events 量）。
    """
    settings = get_graygate_settings()
    today_ms = _window_ms(target_date)
    d1_day = _prev_workday(target_date)       # 上一个工作日
    d2_day = _prev_workday(d1_day)             # 上上个工作日（连续两天判定用）
    d1_ms = _window_ms(d1_day)
    d2_ms = _window_ms(d2_day)

    versions = await resolve_versions(*today_ms)
    ios_v, android_v = versions["ios"], versions["android"]
    if ios_v.top_version is None and android_v.top_version is None:
        return GraygateReportCard(available=False, card={})

    metrics_config = load_metrics_config()
    dashboard_json = await get_dashboard_json(settings.dashboard_id)
    title_index = build_title_index(dashboard_json.get("widgets", []))
    directionality_by_key = {
        spec.key: _widget_directionality(dashboard_json, title_index, spec)
        for spec in metrics_config.metrics
    }

    worsen_candidates: List[_WorsenCandidate] = []
    columns_md: List[str] = []
    for platform, pv in (("ios", ios_v), ("android", android_v)):
        col_md = await _build_platform_column(
            dashboard_json, metrics_config.metrics, platform, pv,
            settings.version_pattern, settings.min_sessions,
            metrics_config.template_variables, today_ms, d1_ms, d2_ms,
            worsen_candidates,
        )
        columns_md.append(col_md)

    worsen_lines = _build_worsen_lines(worsen_candidates, directionality_by_key)

    new_crashes = await find_new_crashes(target_date)
    new_crash_md = _build_new_crash_md(new_crashes)

    top_crashes = await find_top_crashes(target_date)
    top_jank = await find_top_jank(target_date)
    top_crash_md = _build_top_crash_md(top_crashes)
    top_jank_md = _build_top_jank_md(top_jank)

    elements: List[Dict[str, Any]] = [
        _div(
            f"📊 窗口 {target_date.strftime('%m-%d')} 00:00~24:00 BJT · "
            f"基线 {d1_day.strftime('%m-%d')}（上一个工作日）· "
            f"大盘版本模式 `{settings.version_pattern}`"
        ),
    ]

    if worsen_lines:
        elements.append({"tag": "hr"})
        elements.append(_div(
            "**🔴 恶化（核心指标，连续 2 个工作日同向恶化）**\n\n" + "\n".join(worsen_lines)
        ))

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
             "elements": [_div(columns_md[0])]},
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
             "elements": [_div(columns_md[1])]},
        ],
    })

    if new_crash_md:
        elements.append({"tag": "hr"})
        elements.append(_div(new_crash_md))

    if top_crash_md or top_jank_md:
        elements.append({"tag": "hr"})
        if top_crash_md:
            elements.append(_div(top_crash_md))
        if top_jank_md:
            elements.append(_div(top_jank_md))

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "red" if (worsen_lines or new_crash_md) else "turquoise",
            "title": {"tag": "plain_text", "content": f"🆕 [4.0.3 灰度] 每日指标 · {target_date.isoformat()}"},
        },
        "body": {"elements": elements},
    }
    return GraygateReportCard(available=True, card=card)
