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


async def _fetch_tier(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    version_value: Optional[str],
    sample_proxy: int,
    min_sessions: int,
    template_vars_base: Dict[str, str],
    from_ms: int,
    to_ms: int,
) -> Dict[str, _Cell]:
    """给定平台+版本值，对全部指标取一次数。version_value 为 None → 全部 _NO_DATA。"""
    out: Dict[str, _Cell] = {}
    for spec in metrics:
        if spec.not_applicable_platform == platform:
            out[spec.key] = _Cell(value=None, sentinel=_NOT_APPLICABLE)
            continue
        if version_value is None:
            out[spec.key] = _Cell(value=None, sentinel=_NO_DATA)
            continue
        if sample_proxy < min_sessions:
            out[spec.key] = _Cell(value=None, sentinel=_INSUFFICIENT_SAMPLE)
            continue
        out[spec.key] = await _resolve_cell(
            dashboard_json, spec, platform, version_value, None,
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


async def _build_platform_column(
    dashboard_json: dict,
    metrics: List[MetricSpec],
    platform: str,
    pv: PlatformVersions,
    version_pattern: str,
    min_sessions: int,
    template_vars_base: Dict[str, str],
    from_ms: int,
    to_ms: int,
) -> str:
    lines: List[str] = [f"**{_PLATFORM_LABEL[platform]}**", ""]

    # 大盘
    market_cells = await _fetch_tier(
        dashboard_json, metrics, platform, version_pattern, pv.total_events,
        min_sessions, template_vars_base, from_ms, to_ms,
    )
    lines += _tier_md(f"__大盘（{version_pattern}）__", metrics, market_cells)
    lines.append("")

    # 主要版本（events 最大的 build）
    top_cells: Optional[Dict[str, _Cell]] = None
    if pv.top_version:
        top_cells = await _fetch_tier(
            dashboard_json, metrics, platform, pv.top_version, pv.top_version_events,
            min_sessions, template_vars_base, from_ms, to_ms,
        )
        lines += _tier_md(
            f"__主要版本__ `{pv.top_version}`（{_fmt_n(pv.top_version_events)} events）",
            metrics, top_cells,
        )
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
        newest_cells = await _fetch_tier(
            dashboard_json, metrics, platform, pv.newest_version, pv.newest_version_events,
            min_sessions, template_vars_base, from_ms, to_ms,
        )
        lines += _tier_md(
            f"__🆕 最新版本__ `{pv.newest_version}`（{_fmt_n(pv.newest_version_events)} events）",
            metrics, newest_cells,
        )
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


async def build_report_card(target_date: date) -> GraygateReportCard:
    """组装 4.0.3 灰度日报 interactive card。target_date 是 BJT 日历日（代表"昨日"）。"""
    settings = get_graygate_settings()
    yesterday_from, yesterday_to = _window_ms(target_date)

    versions = await resolve_versions(yesterday_from, yesterday_to)
    ios_v, android_v = versions["ios"], versions["android"]
    if ios_v.top_version is None and android_v.top_version is None:
        return GraygateReportCard(available=False, card={})

    metrics_config = load_metrics_config()
    dashboard_json = await get_dashboard_json(settings.dashboard_id)

    columns_md: List[str] = []
    for platform, pv in (("ios", ios_v), ("android", android_v)):
        col_md = await _build_platform_column(
            dashboard_json, metrics_config.metrics, platform, pv,
            settings.version_pattern, settings.min_sessions,
            metrics_config.template_variables, yesterday_from, yesterday_to,
        )
        columns_md.append(col_md)

    new_crashes = await find_new_crashes(target_date)
    new_crash_md = _build_new_crash_md(new_crashes)

    elements: List[Dict[str, Any]] = [
        _div(
            f"📊 窗口 {target_date.strftime('%m-%d')} 00:00~24:00 BJT · "
            f"大盘版本模式 `{settings.version_pattern}`"
        ),
        {"tag": "hr"},
        {
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
        },
    ]

    if new_crash_md:
        elements.append({"tag": "hr"})
        elements.append(_div(new_crash_md))

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "red" if new_crash_md else "turquoise",
            "title": {"tag": "plain_text", "content": f"🆕 [4.0.3 灰度] 每日指标 · {target_date.isoformat()}"},
        },
        "body": {"elements": elements},
    }
    return GraygateReportCard(available=True, card=card)
