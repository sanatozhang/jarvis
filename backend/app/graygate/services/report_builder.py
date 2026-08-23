"""Graygate 报告渲染 —— 把 config / dashboard_query / version_resolver /
new_crashes 四个已完成模块的产出组装成一份 4.0.3 灰度日报（lark_md markdown 文本）。

本文件是 graygate 模块集成度最高的一步：不重新实现任何取数逻辑，只做编排 +
渲染。对外唯一入口是 `build_report(target_date)`，签名/返回类型是 Step6/Step7
的既定契约，不要改。

## 编排规则速览（详见 task-5-brief.md，这里只记录代码里不直接体现的设计取舍）

1. 只调用一次 `resolve_versions()`（昨日窗口）。两个平台 `top_version` 都是
   None → 直接返回 `available=False`，不再发起任何 Datadog 查询。
2. "最新版"列的基线复用同一个 build——`top_version` 同时喂给昨日/前日两次查询
   的 `version` 模板变量，不为前日重新枚举主力包。
3. "大盘"列两天都用 `settings.version_pattern` 通配符。
4. 取数地板（`not_applicable_platform` / 该平台该口径 version 为 None / 样本量
   低于 `min_sessions`）在发起查询**之前**判定——命中即跳过 `get_metric_scalar`
   调用直接渲染占位文案，不浪费一次 Datadog 请求（呼应 `dashboard_query.py`/
   `version_resolver.py` 一贯的"能不查就不查"节流风格）。brief 里"即使查询本身
   成功也不展示具体数值"是在解释语义（低样本的数字不可信），不是要求真的发出
   请求再丢弃结果。
5. **恶化判定对"最新版"（top）和"大盘"（market）两个口径都独立判定**（2026-08-19
   review 裁决，见 task-5-report.md 附录）：早期回归可能先在"大盘"（分散的老
   build 聚合）里冒头，还没成为"最新版"主力包的流量占比，只判 top 会漏掉这类
   信号。数据在取数矩阵阶段已经两个口径都取过，这里不增加 Datadog 调用，只是
   多做一次阈值判断。同一 metric+platform 若两个口径都触发，两行都输出、不去
   重合并；每行前缀 `[最新版]`/`[大盘]` 标签消除口径歧义（brief 给的行格式
   `- iOS Crash-free 99.0% ▼ -0.8pp (vs 前日)` 没有口径标签，这是本文件在此
   基础上做的必要扩展）。
6. 双 widget 指标（jank / home_render）的"指标名"：metrics.yaml 只有
   `title_p75`/`title_p90`（各带"（p75）"/"（p90）"后缀），没有一个干净的合并
   展示名字段，这里维护一份 key → 展示名的白名单（`_METRIC_DISPLAY_NAME_OVERRIDES`）。
   若 metrics.yaml 未来加了专门字段，可以删掉这份白名单退回默认取 title。
7. 双 widget 指标的恶化行：p75/p90 任一子值触发恶化就整行标记（brief 原话），
   渲染时把触发的子值都列出来（如 `p75 ▼-22.0% p90 ▲+5.0%`），因为 brief 给的
   单值行格式没有覆盖双值场景，这是本文件做的展示扩展，不影响判定逻辑本身。
8. 折叠区语法用 `<details><summary>`——brief 允许"lark_md 的 `<details>` 或
   等效折叠语法"，且仓库里 `app/services/linear.py` 已经用同样的写法做文本折叠，
   保持风格一致（crashguard/feishu_card.py 的 `collapsible_panel` 是飞书
   interactive card 的 JSON 组件，不适用于这里——本函数产出的是纯 markdown
   字符串，不是卡片 JSON）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from app.graygate.config import get_graygate_settings
from app.graygate.services.dashboard_query import (
    MetricSpec,
    build_title_index,
    get_dashboard_json,
    get_metric_scalar,
    load_metrics_config,
)
from app.graygate.services.new_crashes import find_new_crashes
from app.graygate.services.version_resolver import resolve_versions

_BJT = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger("jarvis.graygate.report_builder")

_PLATFORMS = ("ios", "android")
_SCOPES = ("top", "market")  # "最新版" / "大盘"

_NOT_APPLICABLE = "—（不适用）"
_NO_DATA = "—（无数据）"
_QUERY_FAILED = "—（取数失败）"
_INSUFFICIENT_SAMPLE = "—（样本不足）"

_PLATFORM_LABEL = {"ios": "iOS", "android": "Android"}
_SCOPE_LABEL = {"top": "最新版", "market": "大盘"}

# 见文件头注释第 6 点。
_METRIC_DISPLAY_NAME_OVERRIDES = {
    "jank": "APP单次使用的卡顿次数",
    "home_render": "首页文件列表加载耗时",
}

CellValue = Union[float, Tuple[float, float]]


@dataclass
class GraygateReport:
    available: bool  # False = 两个平台版本枚举都是空 —— 调用方据此只写心跳、不发报告
    markdown: str     # 完整 lark_md 格式报告文本；available=False 时为空字符串


@dataclass
class _Cell:
    """单个 (metric, platform, scope, day) 组合的取数结果。"""

    value: Optional[CellValue]  # 单 widget: float；双 widget: (p75, p90)；不可用时 None
    sentinel: Optional[str]     # 不可用原因的占位文案；value is None 时必然非空


# ---------------------------------------------------------------------------
# 时间窗口
# ---------------------------------------------------------------------------


def _window_ms(day: date) -> Tuple[int, int]:
    """day 当天 BJT 00:00 到次日 00:00，换算成 UTC 毫秒时间戳。"""
    start = datetime.combine(day, time.min, tzinfo=_BJT)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _window_label(day: date) -> str:
    return f"{day.strftime('%m-%d')} 00:00~24:00"


# ---------------------------------------------------------------------------
# 展示名 / 格式化辅助
# ---------------------------------------------------------------------------


def _metric_name(spec: MetricSpec) -> str:
    override = _METRIC_DISPLAY_NAME_OVERRIDES.get(spec.key)
    if override:
        return override
    return spec.title or spec.title_p75 or spec.key


def _format_cell(spec: MetricSpec, cell: _Cell) -> str:
    if cell.sentinel:
        return cell.sentinel
    if isinstance(cell.value, tuple):
        p75, p90 = cell.value
        return spec.cell_format.format(p75=p75, p90=p90)
    return spec.cell_format.format(v=cell.value)


def _format_delta(spec: MetricSpec, delta: float, baseline: float) -> str:
    if spec.cell_format.endswith("%"):
        return f"{delta:+.2f}pp"
    if baseline == 0:
        return f"{delta:+.2f}"
    return f"{delta / baseline * 100:+.1f}%"


def _breach_threshold(spec: MetricSpec, delta: float, baseline: float) -> bool:
    """恶化判定阈值（照抄 coreguard/metrics.yaml 已验证过的默认值）：
    - cell_format 以 "%" 结尾 → 绝对变化 ≥ 0.5 个百分点
    - 其余 → 相对变化 ≥ 20%
    """
    if spec.cell_format.endswith("%"):
        return abs(delta) >= 0.5
    if baseline == 0:
        return False
    return abs(delta / baseline) >= 0.20


def _is_worse(directionality: Optional[str], delta: float) -> bool:
    """delta = 当前 - 基线。directionality 缺失/未知值 → 不参与判定。"""
    if directionality == "increase_better":
        return delta < 0
    if directionality == "decrease_better":
        return delta > 0
    return False


# ---------------------------------------------------------------------------
# directionality（每个指标只查一次，不分平台/版本口径/日期）
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
    widget_def = widgets[idx].get("definition") or {}
    requests = widget_def.get("requests") or []
    if not requests:
        return None
    comparison = requests[0].get("comparison") or {}
    return comparison.get("directionality")


# ---------------------------------------------------------------------------
# 取数矩阵
# ---------------------------------------------------------------------------


def _gate(
    spec: MetricSpec,
    platform: str,
    scope: str,
    pv_by_platform: Dict[str, object],
    min_sessions: int,
    version_pattern: str,
) -> Tuple[Optional[str], Optional[str]]:
    """返回 (version_value, sentinel)。sentinel 非 None 时不应发起查询。"""
    if spec.not_applicable_platform == platform:
        return None, _NOT_APPLICABLE

    pv = pv_by_platform[platform]
    if scope == "top":
        version_value = pv.top_version
        sample_proxy = pv.top_version_events
        if version_value is None:
            return None, _NO_DATA
    else:
        version_value = version_pattern
        sample_proxy = pv.total_events

    if sample_proxy < min_sessions:
        return version_value, _INSUFFICIENT_SAMPLE
    return version_value, None


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

    template_vars = {
        **template_vars_base,
        "service": f"plaud_{platform}",
        "version": version_value,
    }

    # get_metric_scalar 找不到/歧义 widget 标题会 raise ValueError（不是返回
    # None）——2026-08-23 事故：Datadog 看板 owner 手动改了一个 widget 标题，
    # 这个异常没被 catch，直接炸穿 build_report()，连续两天飞书群什么都没收到。
    # 这里把它当成跟"查询失败/无数据"同一档的降级信号：这一格显示"取数失败"，
    # 不该因为一个指标的看板标题漂移，拖垮整份报告的其它几十个指标。
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


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def build_report(target_date: date) -> GraygateReport:
    """组装 4.0.3 灰度日报。target_date 是 BJT 日历日（如 2026-08-18，代表"昨日"）。"""
    settings = get_graygate_settings()

    yesterday_from, yesterday_to = _window_ms(target_date)
    prev_day = target_date - timedelta(days=1)
    prev_from, prev_to = _window_ms(prev_day)

    versions = await resolve_versions(yesterday_from, yesterday_to)
    ios_v = versions["ios"]
    android_v = versions["android"]

    if ios_v.top_version is None and android_v.top_version is None:
        return GraygateReport(available=False, markdown="")

    metrics_config = load_metrics_config()
    dashboard_json = await get_dashboard_json(settings.dashboard_id)
    title_index = build_title_index(dashboard_json.get("widgets", []))

    directionality_by_key = {
        spec.key: _widget_directionality(dashboard_json, title_index, spec)
        for spec in metrics_config.metrics
    }

    pv_by_platform = {"ios": ios_v, "android": android_v}

    # cells[(metric_key, platform, scope, "yesterday"/"prev")] = _Cell
    cells: Dict[Tuple[str, str, str, str], _Cell] = {}

    for spec in metrics_config.metrics:
        for platform in _PLATFORMS:
            for scope in _SCOPES:
                version_value, sentinel = _gate(
                    spec, platform, scope, pv_by_platform,
                    settings.min_sessions, settings.version_pattern,
                )
                for day_key, (fm, tm) in (
                    ("yesterday", (yesterday_from, yesterday_to)),
                    ("prev", (prev_from, prev_to)),
                ):
                    cells[(spec.key, platform, scope, day_key)] = await _resolve_cell(
                        dashboard_json, spec, platform, version_value, sentinel,
                        metrics_config.template_variables, fm, tm,
                    )

    worsen_lines = _build_worsen_lines(metrics_config.metrics, cells, directionality_by_key)
    new_crashes = await find_new_crashes(target_date)
    full_lines = _build_full_metric_lines(metrics_config.metrics, cells)

    markdown = _render_markdown(
        target_date=target_date,
        prev_day=prev_day,
        ios_v=ios_v,
        android_v=android_v,
        version_pattern=settings.version_pattern,
        worsen_lines=worsen_lines,
        new_crashes=new_crashes,
        full_lines=full_lines,
    )
    return GraygateReport(available=True, markdown=markdown)


# ---------------------------------------------------------------------------
# 恶化段
# ---------------------------------------------------------------------------


def _build_worsen_lines(
    specs: List[MetricSpec],
    cells: Dict[Tuple[str, str, str, str], _Cell],
    directionality_by_key: Dict[str, Optional[str]],
) -> List[str]:
    """恶化判定对"最新版"和"大盘"两个口径都独立跑一遍（不只判 top）——一个
    回归可能先在"大盘"（分散的老 build 聚合）里冒头，还没成为"最新版"主力包
    的流量占比，只判 top 会漏掉这类早期信号。同一 metric+platform 若两个口径
    都触发，两行都输出，不去重合并，让读者自己判断是否是同一问题的两种体现。
    """
    lines: List[str] = []
    for spec in specs:
        for platform in _PLATFORMS:
            for scope in _SCOPES:
                today_cell = cells[(spec.key, platform, scope, "yesterday")]
                prev_cell = cells[(spec.key, platform, scope, "prev")]
                if today_cell.value is None or prev_cell.value is None:
                    continue

                directionality = directionality_by_key.get(spec.key)

                if isinstance(today_cell.value, tuple):
                    sub_pairs = [
                        ("p75", today_cell.value[0], prev_cell.value[0]),
                        ("p90", today_cell.value[1], prev_cell.value[1]),
                    ]
                else:
                    sub_pairs = [(None, today_cell.value, prev_cell.value)]

                triggered = []
                for label, cur, base in sub_pairs:
                    delta = cur - base
                    if not _breach_threshold(spec, delta, base):
                        continue
                    if _is_worse(directionality, delta):
                        triggered.append((label, cur, base, delta))

                if not triggered:
                    continue

                name = _metric_name(spec)
                plat_label = _PLATFORM_LABEL[platform]
                scope_tag = f"[{_SCOPE_LABEL[scope]}]"

                if len(sub_pairs) == 1:
                    _, cur, base, delta = triggered[0]
                    arrow = "▼" if delta < 0 else "▲"
                    value_str = spec.cell_format.format(v=cur)
                    delta_str = _format_delta(spec, delta, base)
                    lines.append(
                        f"- {plat_label} {scope_tag} {name} {value_str} {arrow} {delta_str} (vs 前日)"
                    )
                else:
                    p75, p90 = today_cell.value
                    value_str = spec.cell_format.format(p75=p75, p90=p90)
                    sub_parts = []
                    for label, cur, base, delta in triggered:
                        arrow = "▼" if delta < 0 else "▲"
                        sub_parts.append(f"{label} {arrow}{_format_delta(spec, delta, base)}")
                    lines.append(
                        f"- {plat_label} {scope_tag} {name} {value_str} {' '.join(sub_parts)} (vs 前日)"
                    )

    return lines


# ---------------------------------------------------------------------------
# 全量指标折叠段
# ---------------------------------------------------------------------------


def _build_full_metric_lines(
    specs: List[MetricSpec],
    cells: Dict[Tuple[str, str, str, str], _Cell],
) -> List[str]:
    lines: List[str] = []
    for spec in specs:
        parts = []
        for platform in _PLATFORMS:
            for scope in _SCOPES:
                cell = cells[(spec.key, platform, scope, "yesterday")]
                parts.append(
                    f"{_PLATFORM_LABEL[platform]} {_SCOPE_LABEL[scope]} {_format_cell(spec, cell)}"
                )
        lines.append(f"- **{_metric_name(spec)}**：" + " · ".join(parts))
    return lines


# ---------------------------------------------------------------------------
# Markdown 拼装
# ---------------------------------------------------------------------------


def _render_markdown(
    *,
    target_date: date,
    prev_day: date,
    ios_v,
    android_v,
    version_pattern: str,
    worsen_lines: List[str],
    new_crashes: list,
    full_lines: List[str],
) -> str:
    def top_desc(pv, label: str) -> str:
        if pv.top_version is None:
            return f"{label} 主力 —（无数据）"
        return f"{label} 主力 {pv.top_version}（{pv.top_version_events} events）"

    lines: List[str] = [
        f"🆕 [4.0.3 灰度] 每日指标 · {target_date.strftime('%Y-%m-%d')}",
        f"📦 {top_desc(ios_v, 'iOS')} · {top_desc(android_v, 'Android')} · 大盘 {version_pattern}",
        f"📊 窗口 {_window_label(target_date)} BJT · 基线 {_window_label(prev_day)} 同窗口",
    ]

    if worsen_lines:
        lines.append("")
        lines.append("🔴 恶化（DoD 超阈值）")
        lines.extend(worsen_lines)

    if new_crashes:
        lines.append("")
        lines.append("🆕 新增崩溃堆栈")
        for c in new_crashes:
            lines.append(
                f"- {c.platform.upper()} · {c.version} · {c.events_count} events · "
                f"{c.title}[→]({c.datadog_url})"
            )

    lines.append("")
    lines.append("<details>")
    lines.append("<summary>📋 全量指标</summary>")
    lines.append("")
    lines.extend(full_lines)
    lines.append("")
    lines.append("</details>")

    return "\n".join(lines)
