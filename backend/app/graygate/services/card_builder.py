"""Graygate 飞书 interactive card 渲染（v2 schema）。

用户反馈第一版纯文本消息（`send_message(text=...)`）太丑——飞书 `msg_type: text`
不解析 `**加粗**` / `<details>` 折叠，整段消息挤成一坨。改用 interactive card，
照抄 `app/crashguard/services/feishu_card.py` 里已验证过的视觉语言：iOS/Android
双列布局、每列内"大盘 → 主要版本 → 🆕最新版本"三层、🟩/🟥 状态色点。

三层版本口径定义（比 report_builder.py 的两层 top/market 多一层）：
  大盘（4.0.3*）   —— version_pattern 通配符全量聚合
  主要版本         —— events 最大的 build（当前流量主力，即 report_builder 的 "top"）
  🆕 最新版本      —— build 号最大的 build（刚发布，未必已放量；与主要版本相同的 build
                      时不重复取数，只在渲染层标注"与主要版本一致"，省一次 Datadog 查询）

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
# 恶化摘要段（vs 上一个工作日）—— 只列真正超阈值的指标，不在每行加 delta
# ---------------------------------------------------------------------------


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


@dataclass
class _WorsenCandidate:
    platform: str
    tier_label: str
    spec: MetricSpec
    today: _Cell
    baseline: _Cell


def _build_worsen_lines(
    candidates: List[_WorsenCandidate],
    directionality_by_key: Dict[str, Optional[str]],
) -> List[str]:
    lines: List[str] = []
    for c in candidates:
        if c.today.value is None or c.baseline.value is None:
            continue
        directionality = directionality_by_key.get(c.spec.key)
        # 双 widget（jank/home_render）：p75、p90 任一子值触发即整行标记
        if isinstance(c.today.value, tuple):
            pairs = zip(c.today.value, c.baseline.value)
        else:
            pairs = [(c.today.value, c.baseline.value)]
        worst_delta = None
        worst_baseline = None
        for cur, base in pairs:
            delta = cur - base
            if _breach_threshold(c.spec, delta, base) and _is_worse(directionality, delta):
                if worst_delta is None or abs(delta) > abs(worst_delta):
                    worst_delta, worst_baseline = delta, base
        if worst_delta is None:
            continue
        arrow = "▲" if worst_delta > 0 else "▼"
        if isinstance(c.today.value, tuple):
            cur_str = c.spec.cell_format.format(p75=c.today.value[0], p90=c.today.value[1])
        else:
            cur_str = c.spec.cell_format.format(v=c.today.value)
        lines.append(
            f"- {_PLATFORM_LABEL[c.platform]} [{c.tier_label}] {_metric_name(c.spec)} "
            f"{cur_str} {arrow} {_format_delta(c.spec, worst_delta, worst_baseline)}"
        )
    return lines


async def _fetch_tier_both_days(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    version_value: Optional[str],
    sample_proxy: int,
    min_sessions: int,
    template_vars_base: Dict[str, str],
    today_ms: Tuple[int, int],
    baseline_ms: Tuple[int, int],
) -> Tuple[Dict[str, _Cell], Dict[str, _Cell]]:
    """gate 判定一次，今日窗口和基线窗口（上一个工作日）各查一次，用同一个
    version_value——"同一个包比较两天"，不是"两天各自的主力包比较"（对齐
    report_builder.py 已验证过的设计：DoD 对比要 apples-to-apples）。"""
    gates = _gate_tier(metrics, platform, version_value, sample_proxy, min_sessions)
    today_cells = await _resolve_tier_for_window(
        dashboard_json, metrics, platform, gates, template_vars_base, *today_ms,
    )
    baseline_cells = await _resolve_tier_for_window(
        dashboard_json, metrics, platform, gates, template_vars_base, *baseline_ms,
    )
    return today_cells, baseline_cells


async def _build_platform_column(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    pv: PlatformVersions,
    version_pattern: str,
    min_sessions: int,
    template_vars_base: Dict[str, str],
    today_ms: Tuple[int, int],
    baseline_ms: Tuple[int, int],
    worsen_candidates: List[_WorsenCandidate],
) -> str:
    lines: List[str] = [f"**{_PLATFORM_LABEL[platform]}**", ""]

    # 大盘
    market_today, market_baseline = await _fetch_tier_both_days(
        dashboard_json, metrics, platform, version_pattern, pv.total_events,
        min_sessions, template_vars_base, today_ms, baseline_ms,
    )
    lines += _tier_md(f"__大盘（{version_pattern}）__", metrics, market_today)
    lines.append("")
    for spec in metrics:
        worsen_candidates.append(_WorsenCandidate(
            platform, "大盘", spec, market_today[spec.key], market_baseline[spec.key],
        ))

    # 主要版本（events 最大的 build）
    if pv.top_version:
        top_today, top_baseline = await _fetch_tier_both_days(
            dashboard_json, metrics, platform, pv.top_version, pv.top_version_events,
            min_sessions, template_vars_base, today_ms, baseline_ms,
        )
        lines += _tier_md(
            f"__主要版本__ `{pv.top_version}`（{_fmt_n(pv.top_version_events)} events）",
            metrics, top_today,
        )
        for spec in metrics:
            worsen_candidates.append(_WorsenCandidate(
                platform, "主要版本", spec, top_today[spec.key], top_baseline[spec.key],
            ))
    else:
        lines += _tier_md("__主要版本__", metrics, None, _NO_DATA)
    lines.append("")

    # 🆕 最新版本（build 号最大；与主要版本相同则复用数据，不重复查询）
    if pv.newest_version and pv.newest_version == pv.top_version:
        lines += _tier_md(
            f"__🆕 最新版本__ `{pv.newest_version}`（与主要版本一致，数据同上）",
            metrics, None,
        )
    elif pv.newest_version:
        newest_today, newest_baseline = await _fetch_tier_both_days(
            dashboard_json, metrics, platform, pv.newest_version, pv.newest_version_events,
            min_sessions, template_vars_base, today_ms, baseline_ms,
        )
        lines += _tier_md(
            f"__🆕 最新版本__ `{pv.newest_version}`（{_fmt_n(pv.newest_version_events)} events）",
            metrics, newest_today,
        )
        for spec in metrics:
            worsen_candidates.append(_WorsenCandidate(
                platform, "最新版本", spec, newest_today[spec.key], newest_baseline[spec.key],
            ))
    else:
        lines += _tier_md("__🆕 最新版本__", metrics, None, _NO_DATA)

    return "\n".join(lines)


def _div(content: str) -> Dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _build_new_crash_md(crashes: List[NewCrash]) -> Optional[str]:
    if not crashes:
        return None
    lines = ["**🆕 新增崩溃堆栈**", ""]
    for c in crashes:
        lines.append(
            f"- {c.platform.upper()} · `{c.version}` · **{c.events_count}** events · "
            f"{c.title}[→]({c.datadog_url})"
        )
    return "\n".join(lines)


def _build_top_crash_md(crashes: List[TopCrash]) -> Optional[str]:
    if not crashes:
        return None
    lines = ["**🔥 Top 5 崩溃（按 events，不限是否新增）**", ""]
    for i, c in enumerate(crashes, 1):
        lines.append(
            f"{i}. {c.platform.upper()} · **{_fmt_n(c.events_count)}** events · "
            f"{c.title}[→]({c.datadog_url})"
        )
    return "\n".join(lines)


def _build_top_jank_md(janks: List[TopJank]) -> Optional[str]:
    if not janks:
        return None
    lines = ["**🟠 Top 5 卡顿（按 events，不限是否新增）**", ""]
    for i, j in enumerate(janks, 1):
        lines.append(f"{i}. {j.platform.upper()} · **{_fmt_n(j.events_count)}** events · `{j.label}`")
    return "\n".join(lines)


async def build_report_card(target_date: date) -> GraygateReportCard:
    """组装 4.0.3 灰度日报 interactive card。target_date 是 BJT 日历日（代表"昨日"）。

    结构（自上而下）：header → 🔴 恶化摘要（vs 上一个工作日，有才出）→
    iOS/Android 双列版本数据（大盘/主要版本/🆕最新版本）→ 🆕 新增崩溃堆栈（有才出）
    → 🔥 Top5 崩溃 + 🟠 Top5 卡顿（有才出，不看是否新增，按 events 量）。
    """
    settings = get_graygate_settings()
    today_ms = _window_ms(target_date)
    baseline_day = _prev_workday(target_date)
    baseline_ms = _window_ms(baseline_day)

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
            metrics_config.template_variables, today_ms, baseline_ms,
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
            f"基线 {baseline_day.strftime('%m-%d')}（上一个工作日）· "
            f"大盘版本模式 `{settings.version_pattern}`"
        ),
    ]

    if worsen_lines:
        elements.append({"tag": "hr"})
        elements.append(_div("**🔴 恶化（vs 上一个工作日）**\n\n" + "\n".join(worsen_lines)))

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
