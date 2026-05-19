"""
飞书 Interactive Card 构造器（早晚报 + hourly 告警）。

输入 daily_report.compose_report() 生成的 markdown 已不够用——飞书富文本卡片需要结构化数据。
为此，我们直接基于 compose_report 的 payload + 复用一些原始数据，构造卡片 schema。

为了简化：把整篇 markdown 拆成段（## 大标题 → header），其余作为 lark_md content；
最后加一个"在 Web 查看完整报告"按钮。

Hourly 告警卡片复用同样的色板和 layout 风格——新增/上涨用 red template，
聚合 digest 一张卡，按 events 量/上涨比 desc 排序。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List


def _split_sections(markdown: str) -> List[Dict[str, str]]:
    """按 ## 标题切段。返回 [{title, content}]，第一段无 title 则归到 _intro。"""
    sections: List[Dict[str, str]] = []
    cur_title = ""
    cur_lines: List[str] = []
    for line in markdown.split("\n"):
        if line.startswith("## "):
            if cur_lines:
                sections.append({"title": cur_title, "content": "\n".join(cur_lines).strip()})
            cur_title = line[3:].strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_lines:
        sections.append({"title": cur_title, "content": "\n".join(cur_lines).strip()})
    return sections


def _truncate_md(content: str, limit: int = 3500) -> str:
    if len(content) > limit:
        return content[:limit] + "\n\n_…内容过长，已截断，详见 Web 端_"
    return content


def _div(content: str) -> Dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _collapsible_panel(
    title_md: str,
    elements: List[Dict[str, Any]],
    expanded: bool = False,
    background: str = "grey-100",
) -> Dict[str, Any]:
    """飞书 v2 card collapsible_panel 组件——折叠区。

    底层逻辑：把 FYI 内容折叠，默认收起；点击 header 展开全文。
    需要 schema 2.0；与 div/hr/action 同级。
    """
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "background_color": background,
        "header": {
            "title": {"tag": "markdown", "content": title_md},
            "vertical_align": "center",
            "padding": "4px 0px 4px 8px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "neutral",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "elements": elements,
    }


def _tldr_headline(tldr: Dict[str, Any]) -> str:
    """从 tldr.platforms 拼一行平台状态——每平台一段 chip。"""
    parts: List[str] = []
    for p in tldr.get("platforms", []) or []:
        label = p.get("platform_label") or "?"
        status = p.get("status") or "unknown"
        delta = p.get("delta_pct")
        new_n = int(p.get("new_count") or 0)
        if status == "red":
            tag = "🔴"
        elif status == "yellow":
            tag = "🟡"
        elif status == "green_improve":
            tag = "✅"
        elif status == "green":
            tag = "✅"
        else:
            tag = "⚪"
        # 文案：平台 fatal +N% / 持平 / 改善 / 无基线
        if status in ("red", "yellow"):
            delta_str = f"fatal +{delta:.0f}%" if delta is not None else "fatal 上涨"
            if new_n > 0:
                delta_str += f" · 🆕{new_n}"
            parts.append(f"{label} {delta_str} {tag}")
        elif status == "green_improve":
            parts.append(f"{label} fatal {delta:.0f}% ✅")
        elif status == "green":
            parts.append(f"{label} 持平 {tag}")
        else:
            # unknown：无基线
            parts.append(f"{label} —")
    return " · ".join(parts) if parts else "全平台无数据"


def _build_tldr_elements(
    tldr: Dict[str, Any],
    frontend_base_url: str,
    fallback_summary_md: str,
) -> List[Dict[str, Any]]:
    """三行 TL;DR：①今日重点 ②👉 必看 ③其他 N 项无需立刻动。"""
    if not tldr:
        # 兼容老 payload（无 tldr 字段）
        return [_div(fallback_summary_md)]

    severity = tldr.get("severity") or "green"
    headline = _tldr_headline(tldr)
    must_see = tldr.get("must_see")
    other_count = int(tldr.get("other_count") or 0)
    anomaly_total = int(tldr.get("anomaly_total") or 0)

    if severity == "red":
        prefix = "🚨 **今日重点**"
    elif severity == "yellow":
        prefix = "🟡 **今日重点**"
    else:
        prefix = "🌿 **今日重点**"

    line1 = f"{prefix}：{headline}"
    elements: List[Dict[str, Any]] = [_div(line1)]

    if must_see:
        title = must_see.get("title") or must_see.get("issue_id", "")
        url = must_see.get("url") or f"{frontend_base_url.rstrip('/')}/crashguard"
        ev = int(must_see.get("events") or 0)
        delta_pct = must_see.get("delta_pct")
        plat = must_see.get("platform") or ""
        if must_see.get("is_new"):
            extra = "新版首现"
        elif delta_pct is not None:
            extra = f"{'+' if delta_pct >= 0 else ''}{delta_pct:.0f}% vs 上周"
        else:
            extra = ""
        plat_str = f"[{plat}] " if plat else ""
        extra_str = f", {extra}" if extra else ""
        line2 = (
            f"👉 **必看**：{plat_str}[{title}]({url}) "
            f"（{ev:,} events{extra_str}）"
        )
        elements.append(_div(line2))

    # 文案三态：
    #   ① 无单 issue 异常但平台级 fatal 红/黄（severity != green）
    #      → "单 issue 无突增，平台级 fatal 波动看下方分平台明细"（避免与 TL;DR 红头矛盾）
    #   ② 无单 issue 异常且平台 green → "全平台 fatal 平稳"
    # 注：原"其他 N 项无需立刻动"提示已下线——口径相关文字统一去 docs/crashguard/metrics-glossary.md 查
    if anomaly_total == 0 and severity in ("red", "yellow"):
        line3 = (
            "> 单 issue 未突破 ±10% 突增阈值，但平台级 fatal 已波动 —— "
            "看下方 **分平台明细** 找根因。"
        )
    elif anomaly_total == 0:
        line3 = "> 全平台 fatal 平稳，无需关注。"
    else:
        line3 = ""
    if line3:
        elements.append(_div(line3))

    return elements


def _cf_emoji(pct: float) -> str:
    if pct >= 99.9:
        return "🟩"
    if pct >= 99.5:
        return "🟨"
    return "🟥"


def _fmt_n(n: Any) -> str:
    if n is None:
        return "—"
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def _build_crash_free_column_md(
    plat_label: str,
    all_stats: Dict[str, Any] | None,
    ver_stats: Dict[str, Any] | None,
    wow: Dict[str, Any] | None = None,
    latest_stats: Dict[str, Any] | None = None,
) -> str:
    """单平台一栏的 markdown（全版本 + 主要版本 + 最新版本三段）。
    wow: dual_window platform dict with today_fatal/baseline_fatal/fatal_delta_pct/sess_delta_pct.
    latest_stats: 最新版本 crash-free stats dict.
    """
    lines: List[str] = [f"**{plat_label}**", ""]
    if all_stats:
        pct = all_stats.get("crash_free_pct")
        pct_str = (
            f"{_cf_emoji(float(pct))} **{float(pct):.2f}%**" if pct is not None else "—"
        )
        lines.append("__全版本（大盘，已结束会话）__")
        lines.append(f"· 会话总数：**{_fmt_n(all_stats.get('total_sessions'))}**")
        lines.append(f"· Crash-free：{_fmt_n(all_stats.get('crash_free_sessions'))}")
        lines.append(f"· 崩溃会话：{_fmt_n(all_stats.get('crashed_sessions'))}")
        lines.append(f"· Crash-free 率：{pct_str}")
        # ANR / App Hang 明细（来自 breakdown，error 事件数）
        bd = all_stats.get("breakdown") or {}
        anr = bd.get("anr") or bd.get("ANR")
        hang = bd.get("app_hang") or bd.get("App Hang")
        native = bd.get("native_crash")
        breakdown_parts = []
        if native:
            breakdown_parts.append(f"native {_fmt_n(native)}")
        if anr:
            breakdown_parts.append(f"ANR {_fmt_n(anr)}")
        if hang:
            breakdown_parts.append(f"Hang {_fmt_n(hang)}")
        if breakdown_parts:
            lines.append(f"· 崩溃明细：{' · '.join(breakdown_parts)}")
        # WoW 对比（来自双窗口数据，只看 fatal 趋势）
        if wow:
            sess_pct = wow.get("sess_delta_pct")
            fat_pct = wow.get("fatal_delta_pct")
            def _wow_str(v):
                if v is None:
                    return "—"
                sign = "+" if v >= 0 else ""
                return f"{sign}{v:.0f}%"
            lines.append(
                f"· vs 上周：sessions {_wow_str(sess_pct)} · fatal {_wow_str(fat_pct)}"
            )
        lines.append("")
    if ver_stats:
        pct = ver_stats.get("crash_free_pct")
        pct_str = (
            f"{_cf_emoji(float(pct))} **{float(pct):.2f}%**" if pct is not None else "—"
        )
        ver = ver_stats.get("version") or "—"
        lines.append(f"__主要版本__ `{ver}`")
        spp = ver_stats.get("share_of_platform_pct")
        sap = ver_stats.get("share_of_all_pct")
        if spp is not None:
            lines.append(f"· 占平台：{spp:.2f}% · 占全部：{sap:.2f}%")
        lines.append(f"· 会话总数：**{_fmt_n(ver_stats.get('total_sessions'))}**")
        lines.append(f"· Crash-free：{_fmt_n(ver_stats.get('crash_free_sessions'))}")
        lines.append(f"· 崩溃：{_fmt_n(ver_stats.get('crashed_sessions'))}")
        lines.append(f"· Crash-free 率：{pct_str}")
        lines.append("")
    if latest_stats:
        pct = latest_stats.get("crash_free_pct")
        pct_str = (
            f"{_cf_emoji(float(pct))} **{float(pct):.2f}%**" if pct is not None else "—"
        )
        ver = latest_stats.get("version") or "—"
        lines.append(f"__🆕 最新版本__ `{ver}`")
        spp = latest_stats.get("share_of_platform_pct")
        sap = latest_stats.get("share_of_all_pct")
        if spp is not None:
            lines.append(f"· 占平台：{spp:.2f}% · 占全部：{sap:.2f}%")
        lines.append(f"· 会话总数：**{_fmt_n(latest_stats.get('total_sessions'))}**")
        lines.append(f"· Crash-free：{_fmt_n(latest_stats.get('crash_free_sessions'))}")
        lines.append(f"· 崩溃：{_fmt_n(latest_stats.get('crashed_sessions'))}")
        lines.append(f"· Crash-free 率：{pct_str}")
    return "\n".join(lines)


def _build_summary_md(
    label: str,
    all_summary: Dict[str, Any] | None,
    ver_summary: Dict[str, Any] | None,
) -> str:
    """iOS + Android 汇总——横向一行紧凑."""
    parts: List[str] = []
    if all_summary:
        pct = all_summary.get("crash_free_pct")
        pct_str = (
            f"{_cf_emoji(float(pct))} **{float(pct):.2f}%**" if pct is not None else "—"
        )
        parts.append(
            f"**全版本汇总** · 会话 {_fmt_n(all_summary.get('total_sessions'))} · "
            f"崩溃 {_fmt_n(all_summary.get('crashed_sessions'))} · {pct_str}"
        )
    if ver_summary:
        pct = ver_summary.get("crash_free_pct")
        pct_str = (
            f"{_cf_emoji(float(pct))} **{float(pct):.2f}%**" if pct is not None else "—"
        )
        sap = ver_summary.get("share_of_all_pct")
        sap_str = f"（占全部 {sap:.2f}%）" if sap is not None else ""
        parts.append(
            f"**主要版本汇总**{sap_str} · 会话 {_fmt_n(ver_summary.get('total_sessions'))} · "
            f"崩溃 {_fmt_n(ver_summary.get('crashed_sessions'))} · {pct_str}"
        )
    return "\n\n".join(parts) if parts else ""


def _build_dual_window_columns(dw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """飞书 v2 column_set：双窗口对照——iOS 左 / Android 右 / 合计下方一行。

    抓手：sessions + fatal events 两个口径，「今/上周→Δ」一栏一行，视觉对比 2 秒读完。
    """
    plats = dw.get("platforms") or {}
    sumr = dw.get("summary") or {}

    def _fatal_tag(delta_pct):
        if delta_pct is None:
            return ""
        if delta_pct >= 50:
            return " 🔴"
        if delta_pct <= -10:
            return " ✅"
        return ""

    def _delta(today, base, delta_pct):
        sign = "+" if (delta_pct is not None and delta_pct >= 0) else ""
        d_str = f"{sign}{delta_pct:.0f}%" if delta_pct is not None else "—"
        if (base == 0 and today == 0):
            return f"{_fmt_n(today)} / {_fmt_n(base)} → —"
        return f"**{_fmt_n(today)}** vs {_fmt_n(base)} → **{d_str}**"

    def _col_md(label: str, p: Dict[str, Any]) -> str:
        sess_line = _delta(
            p.get("today_sessions", 0), p.get("baseline_sessions", 0), p.get("sess_delta_pct")
        )
        fatal_pct = p.get("fatal_delta_pct")
        fatal_line = _delta(
            p.get("today_fatal", 0), p.get("baseline_fatal", 0), fatal_pct,
        ) + _fatal_tag(fatal_pct)
        return (
            f"**{label}**\n\n"
            f"__sessions__\n{sess_line}\n\n"
            f"__fatal events__\n{fatal_line}"
        )

    ios = plats.get("IOS") or {}
    and_ = plats.get("ANDROID") or {}

    out: List[Dict[str, Any]] = []
    out.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "default",
        "columns": [
            {
                "tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
                "elements": [_div(_col_md("🍎 iOS", ios))],
            },
            {
                "tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
                "elements": [_div(_col_md("📱 Android", and_))],
            },
        ],
    })
    # 合计行
    if sumr:
        sess_line = _delta(
            sumr.get("today_sessions", 0), sumr.get("baseline_sessions", 0), sumr.get("sess_delta_pct"),
        )
        fp = sumr.get("fatal_delta_pct")
        fatal_line = _delta(
            sumr.get("today_fatal", 0), sumr.get("baseline_fatal", 0), fp,
        ) + _fatal_tag(fp)
        out.append(_div(
            f"**📊 合计** · sessions {sess_line}  ·  fatal {fatal_line}"
        ))
    out.append(_div(
        "> 💡 fatal Δ 高于 sessions Δ = crash rate 真恶化；反之 = 质量改善"
    ))
    return out


def _build_crash_free_columns(detail: Dict[str, Any], dual_window: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """飞书 v2 column_set 双列：左 iOS / 右 Android，下方一行汇总。

    dual_window: 双窗口 payload，用于在全版本块追加 vs 上周 WoW 对比行。
    """
    all_block = (detail.get("all_versions") or {})
    ver_block = (detail.get("top_user_versions") or {})
    latest_block = (detail.get("latest_versions") or {})
    all_plats = all_block.get("platforms") or {}
    ver_plats = ver_block.get("platforms") or {}
    latest_plats = latest_block.get("platforms") or {}
    dw_plats = (dual_window or {}).get("platforms") or {}

    out: List[Dict[str, Any]] = []

    # 口径说明已迁移至 docs/crashguard/metrics-glossary.md（早晚报不再赘述）

    # 双列：iOS 左 / Android 右
    ios_md = _build_crash_free_column_md(
        "🍎 iOS", all_plats.get("IOS"), ver_plats.get("IOS"),
        wow=dw_plats.get("IOS"),
        latest_stats=latest_plats.get("IOS"),
    )
    and_md = _build_crash_free_column_md(
        "📱 Android", all_plats.get("ANDROID"), ver_plats.get("ANDROID"),
        wow=dw_plats.get("ANDROID"),
        latest_stats=latest_plats.get("ANDROID"),
    )

    out.append({
        "tag": "column_set",
        "flex_mode": "stretch",
        "background_style": "default",
        "horizontal_spacing": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [_div(ios_md)],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [_div(and_md)],
            },
        ],
    })

    # 汇总行（横向，不分列）
    summary_md = _build_summary_md(
        "iOS + Android",
        all_block.get("summary"),
        ver_block.get("summary"),
    )
    if summary_md:
        out.append(_div(summary_md))
    return out


def build_daily_card(
    report_type: str,
    target_date: str,
    markdown: str,
    payload: Dict[str, Any],
    frontend_base_url: str = "http://localhost:3000",
) -> Dict[str, Any]:
    """构造飞书 interactive card payload（v2 schema）。

    顶层设计：
    1. **TL;DR 区**（顶置，不折叠）—— 一眼速读今日重点 + 必看 issue + 其他无需关注数
    2. **数据口径 banner**（小行，inline 在 TL;DR 下方）
    3. **🆕 今日关注点 / 新增 / 突增**段（默认展开，必看区）
    4. **collapsible_panel × N**（FYI 折叠）：双窗口对照 / 各平台 详情 / 下降 / 复盘
    5. Action 按钮（Web 端）

    抓手：把 80% 行动力压到第一屏，FYI 折叠到下方需要时再点开。
    """
    is_morning = report_type == "morning"
    new_count = int(payload.get("new_count") or 0)
    surge_count = int(payload.get("surge_count") or 0)
    drop_count = int(payload.get("regression_count") or 0)
    has_anomaly = (new_count + surge_count + drop_count) > 0
    tldr = payload.get("tldr") or {}

    # 卡片头部颜色：优先 tldr.severity；fallback 老逻辑
    severity = tldr.get("severity")
    if severity == "red":
        template = "red"
    elif severity == "yellow":
        template = "yellow"
    elif severity == "green":
        template = "turquoise"
    else:
        template = "red" if has_anomaly else "turquoise"

    # 早晚报差异化
    evening_window_h = int(payload.get("data_window_hours") or 10)
    if is_morning:
        title_text = f"🌅 Crashguard 日报 · {target_date}"
        scope_md = (
            f"📊 **数据口径**：过去 **24h**（昨日总览） · "
            f"基线：**上周同 weekday 同 24h 段**（SHoW-24h）"
        )
    else:
        title_text = f"🌇 Crashguard 速报 · {target_date}"
        scope_md = (
            f"📊 **数据口径**：过去 **{evening_window_h}h**（日内增量） · "
            f"基线：**上周同 weekday 同 {evening_window_h}h 段**（SHoW-{evening_window_h}h）"
        )

    # 顶层 summary 老 fallback（无 tldr 字段时使用）
    fallback_summary_md = (
        f"**Σ** 新增 **{new_count}** · 突增 **{surge_count}** · 下降 **{drop_count}**"
        if has_anomaly
        else "🌿 **数据平稳，安全无虞**"
    )

    elements: List[Dict[str, Any]] = []

    # ── TL;DR 顶置区（不折叠）──
    elements.extend(_build_tldr_elements(tldr, frontend_base_url, fallback_summary_md))
    # 口径 banner 紧跟 TL;DR（小字体，引用样式）
    elements.append(_div(f"> {scope_md}"))
    elements.append({"tag": "hr"})

    # ── 按 markdown 切段：必看段（关注点/新增/突增）展开，FYI 段折叠 ──
    sections = _split_sections(markdown)

    # 主题分类：标题里含这些关键字 → 必看（默认展开）；其余 → 折叠
    EXPANDED_KEYWORDS = ("关注", "新增", "突增", "TL;DR", "Crash-free 详表")

    crash_free_detail = payload.get("crash_free_detail") or {}
    dual_window = payload.get("dual_window") or {}

    for sec in sections:
        title = sec["title"]
        content = sec["content"]
        if not title and not content:
            continue
        if not title:
            # 无标题段（首段 intro）—— 跳过，已被 TL;DR 替代
            continue

        # 拦截 Crash-free 详表：飞书 lark_md 不支持 table，改用 column_set 双列原生布局
        # 同时注入 dual_window WoW 对比（替代已移除的「双窗口对照」section）
        if "Crash-free 详表" in title and crash_free_detail:
            elements.append(_div(f"**{title}**"))
            elements.extend(_build_crash_free_columns(crash_free_detail, dual_window=dual_window))
            elements.append({"tag": "hr"})
            continue

        # 双窗口对照已合并入 Crash-free 详表，直接跳过不渲染
        if "双窗口对照" in title:
            continue

        sec_elements: List[Dict[str, Any]] = []
        if content:
            sec_elements.append(_div(_truncate_md(content)))

        is_expanded = any(kw in title for kw in EXPANDED_KEYWORDS)
        if is_expanded:
            # 不折叠：直接平铺标题 + 内容
            elements.append(_div(f"**{title}**"))
            elements.extend(sec_elements)
            elements.append({"tag": "hr"})
        else:
            # 折叠区：标题前加 ▶ 视觉提示（v1 client 兜底）
            panel_title = f"▶ **{title}**"
            elements.append(
                _collapsible_panel(panel_title, sec_elements, expanded=False)
            )

    # ── 底部按钮（v2 schema：button 直接作为 element，不再 wrap 在 action 里）──
    elements.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "📊 在 Web 端查看 / 操作"},
        "type": "primary",
        "behaviors": [
            {
                "type": "open_url",
                "default_url": f"{frontend_base_url.rstrip('/')}/crashguard",
            },
        ],
    })

    # 飞书 v2.0 schema：collapsible_panel 必须挂 body.elements 下，顶层不允许 elements。
    # 若顶层同时给 elements 会被 v2 校验器拒（200621 parse card json err）。
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title_text},
        },
        "body": {"elements": elements},
    }


def _platform_emoji(p: str) -> str:
    p = (p or "").lower()
    return {"android": "🤖", "ios": "🍎", "flutter": "🎯"}.get(p, "📱")


def build_hourly_alert_card(
    *,
    hour_utc: datetime,
    new_items: List[Dict[str, Any]],
    surge_items: List[Dict[str, Any]],
    new_version_items: List[Dict[str, Any]] = None,
    new_crash_items: List[Dict[str, Any]] = None,
    threshold_pct: float = 10.0,
    frontend_base_url: str = "http://localhost:3000",
    alert_id: int | None = None,
) -> Dict[str, Any]:
    """构造 hourly 告警 interactive card payload。

    复用早晚报色板：异常 → red header，平稳 → 不该走到这里。
    聚合 digest 一张卡，避免高频刷屏。
    严格不含 PR 修复内容——按用户要求，PR 状态查看走前端。
    """
    new_version_items = new_version_items or []
    new_crash_items = new_crash_items or []
    new_n = len(new_items or [])
    surge_n = len(surge_items or [])
    nv_n = len(new_version_items)
    nc_n = len(new_crash_items)
    # 显示用新加坡时区（UTC+8）—— Plaud 主用户群体所在时区
    from datetime import timedelta as _td
    sg_dt = hour_utc + _td(hours=8)
    hour_label = sg_dt.strftime("%Y-%m-%d %H:%M SGT")
    template = "red"  # 触发到这里必有异常
    title_text = f"🚨 Crashguard 实时告警 · {hour_label}"

    elements: List[Dict[str, Any]] = []

    # 顶部摘要
    summary_md = (
        f"**Σ** 过去 3 小时 · 新增 **{new_n}** · 上涨 **{surge_n}**"
        f" · 新版本 **{nv_n}** · 新crash **{nc_n}**  ·  "
        f"阈值 events +{threshold_pct:.0f}% **AND** rate 同步涨（对比上周同 3h 块，SHoW-3h）"
    )
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": summary_md},
    })
    elements.append({"tag": "hr"})

    # === [新版本] 灰度异常段 ===
    if new_version_items:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**🔴 [新版本] 灰度异常 · {nv_n} 项**"},
        })
        for idx, it in enumerate(new_version_items, 1):
            pe = _platform_emoji(it.get("platform", ""))
            url = f"{frontend_base_url.rstrip('/')}/crashguard?issue={it['issue_id']}"
            first_ver = it.get("first_seen_version") or "—"
            user_rate = it.get("user_rate_pct", 0)
            content = (
                f"{idx}. {pe} [{it.get('title') or it['issue_id']}]({url})\n"
                f"   版本: {it.get('version') or '—'} | 首次出现: {first_ver}\n"
                f"   3h events: {it.get('events_h', 0)} | sessions: {it.get('sessions_h', 0)}"
                f" | user_rate: {user_rate}%"
            )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            })
        elements.append({"tag": "hr"})

    # === [新 crash] 全网首现段 ===
    if new_crash_items:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**🟠 [新 crash] 全网首现 · {nc_n} 项**"},
        })
        for idx, it in enumerate(new_crash_items, 1):
            pe = _platform_emoji(it.get("platform", ""))
            url = f"{frontend_base_url.rstrip('/')}/crashguard?issue={it['issue_id']}"
            first_ver = it.get("first_seen_version") or "—"
            first_at = it.get("first_seen_at") or "—"
            content = (
                f"{idx}. {pe} [{it.get('title') or it['issue_id']}]({url})\n"
                f"   首次出现版本: {first_ver} | 首现时间: {first_at}\n"
                f"   24h events: {it.get('events_24h', 0)} | sessions: {it.get('sessions_24h', 0)}"
            )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            })
        elements.append({"tag": "hr"})

    # 新增段
    if new_items:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**🆕 新增崩溃（近 30 天首现）· {new_n} 项**"},
        })
        new_lines: List[str] = []
        for it in new_items:
            url = f"{frontend_base_url.rstrip('/')}/crashguard?issue={it['issue_id']}"
            pe = _platform_emoji(it.get("platform", ""))
            sess = it.get("sessions_h") or 0
            sess_str = f" · {sess} 会话" if sess else ""
            new_lines.append(
                f"- {pe} [{it.get('title') or it['issue_id']}]({url})  ·  **{it['events_h']}** events{sess_str}"
            )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(new_lines)},
        })
        elements.append({"tag": "hr"})

    # 上涨段
    if surge_items:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**📈 异常上涨 · {surge_n} 项（vs 上周同时段）**"},
        })
        surge_lines: List[str] = []
        for it in surge_items:
            url = f"{frontend_base_url.rstrip('/')}/crashguard?issue={it['issue_id']}"
            pe = _platform_emoji(it.get("platform", ""))
            src = "SHoW" if it.get("baseline_source") == "show" else "7d 均值"
            sess = it.get("sessions_h") or 0
            sess_str = f" · {sess} 会话" if sess else ""
            # rate 维度：events/sessions × 100；可缺失（老 snapshot / API 空）→ 不显示
            rate_now = it.get("rate_now")
            rate_growth = it.get("rate_growth_pct")
            if rate_now is not None and rate_growth is not None:
                rate_str = f"  ·  rate **{rate_now:.2f}%** ({'+' if rate_growth >= 0 else ''}{rate_growth:.1f}%)"
            else:
                rate_str = ""
            surge_lines.append(
                f"- {pe} [{it.get('title') or it['issue_id']}]({url})  ·  "
                f"**{it['events_h']}** vs {it['baseline']:.0f} ({src})  ·  "
                f"**+{it['growth_pct']:.1f}%** ⬆️{rate_str}{sess_str}"
            )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(surge_lines)},
        })
        elements.append({"tag": "hr"})

    # 底部按钮：path 化深链，直达本条告警详情页（不再走 list+modal）
    if alert_id is not None:
        btn_url = f"{frontend_base_url.rstrip('/')}/crashguard/alerts/hourly/{alert_id}"
    else:
        btn_url = f"{frontend_base_url.rstrip('/')}/crashguard/reports?type=hourly_alert"
    action_buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📊 Web 端查看"},
            "type": "primary",
            "url": btn_url,
        },
    ]
    if alert_id is not None:
        feedback_base = (
            f"{frontend_base_url.rstrip('/')}/api/crash/alert-feedback?alert_id={alert_id}"
        )
        action_buttons.extend([
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "👍 准"},
                "type": "default",
                "url": f"{feedback_base}&label=good",
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "👎 不准"},
                "type": "danger",
                "url": f"{feedback_base}&label=bad",
            },
        ])
    elements.append({"tag": "action", "actions": action_buttons})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title_text},
        },
        "elements": elements,
    }


def build_core_metric_alert_card(
    window_start: datetime,
    items: List[Dict[str, Any]],
    threshold_pp: float = 0.3,
    frontend_base_url: str = "http://localhost:3000",
    alert_id: int | None = None,
) -> Dict[str, Any]:
    """核心指标报警卡片（crash-free sessions % 健康度告警）。

    items: [{platform, crash_free_pct, baseline_pct, delta_pp, direction,
             total_sessions, crashed_sessions}, ...]
    direction down=crash-free 跌（坏消息，红）；up=反弹（信号意义，黄）。
    """
    from datetime import timedelta as _td
    sg_dt = window_start + _td(hours=8)
    window_label = sg_dt.strftime("%Y-%m-%d %H:%M SGT")

    has_down = any(it.get("direction") == "down" for it in items)
    template = "red" if has_down else "yellow"
    title_text = f"📉 Crashguard 核心指标告警 · {window_label}"

    _DIM_LABEL = {
        "overall":        "📊 大盘",
        "main_version":   "👥 主要版本",
        "latest_version": "🆕 最新版本",
    }

    elements: List[Dict[str, Any]] = []
    summary_md = (
        f"**Σ** 30 分钟滑动均值 · 触发 **{len(items)}** 条  ·  "
        f"阈值 ±{threshold_pp:.2f} pp（vs 前 1h 加权均值）  ·  崩溃数 ≥ 10"
    )
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": summary_md}})
    elements.append({"tag": "hr"})

    # 按维度分组展示
    from itertools import groupby as _groupby
    dim_order = ["overall", "main_version", "latest_version"]
    items_sorted = sorted(items, key=lambda x: dim_order.index(x.get("dimension", "overall"))
                          if x.get("dimension", "overall") in dim_order else 99)

    last_dim = None
    for it in items_sorted:
        dim = it.get("dimension", "overall")
        if dim != last_dim:
            dim_label = _DIM_LABEL.get(dim, dim)
            ver = it.get("version_tag", "")
            dim_header = f"**{dim_label}**" + (f"  `{ver}`" if ver else "")
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": dim_header}})
            last_dim = dim

        pe = _platform_emoji(it.get("platform", ""))
        direction = it.get("direction", "")
        arrow = "🔻" if direction == "down" else "🔺"
        delta = it.get("delta_pp", 0.0)
        sign = "+" if delta >= 0 else ""
        platform_label = (it.get("platform") or "").upper() or "?"
        line = (
            f"{pe} **{platform_label}**  ·  "
            f"crash-free **{it.get('crash_free_pct', 0):.2f}%** "
            f"(基线 {it.get('baseline_pct', 0):.2f}%)  ·  "
            f"{arrow} **{sign}{delta:.2f} pp**\n"
            f"  会话 {it.get('total_sessions', 0)} · "
            f"崩溃 {it.get('crashed_sessions', 0)}"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
    elements.append({"tag": "hr"})

    if alert_id is not None:
        btn_url = (
            f"{frontend_base_url.rstrip('/')}/crashguard/reports"
            f"?type=core_metric_alert&alert_id={alert_id}"
        )
    else:
        btn_url = f"{frontend_base_url.rstrip('/')}/crashguard/reports?type=core_metric_alert"
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📊 在 Web 端查看"},
            "type": "primary",
            "url": btn_url,
        }],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title_text},
        },
        "elements": elements,
    }


def build_job_health_alert_card(
    items: List[Dict[str, Any]],
    cooldown_minutes: int = 30,
    frontend_base_url: str = "http://localhost:3000",
) -> Dict[str, Any]:
    """定时任务健康度告警卡片。

    items: [{job_name, health (failing/stale), consecutive_failures, last_error,
             last_fired_at, last_success_at, ...}]
    health=stale → 超期未跑；health=failing → 连续 ≥3 次失败
    """
    title_text = f"⚙️ Crashguard 定时任务异常 · {len(items)} 项需关注"
    elements: List[Dict[str, Any]] = []

    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"📊 **检测窗口**：每 5 分钟扫描心跳表 · "
                f"同任务节流 **{cooldown_minutes} 分钟**（避免刷屏）"
            ),
        },
    })
    elements.append({"tag": "hr"})

    for it in items:
        h = it.get("health", "")
        health_emoji = "🔴" if h == "failing" else "⏰"
        health_label = "连续失败" if h == "failing" else "超期未跑"
        last_err = (it.get("last_error") or "")
        err_line = f"\n  ⚠️ 最近错误：`{last_err}`" if last_err and h == "failing" else ""
        last_success = it.get("last_success_at") or "—"
        cf = it.get("consecutive_failures") or 0
        interval = it.get("interval_minutes")
        interval_str = f"{interval}min" if interval else "—"
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"{health_emoji} **{it.get('job_name')}** · {health_label}\n"
                    f"  连续失败 **{cf}** 次 · 预期间隔 {interval_str}\n"
                    f"  上次成功：{last_success}{err_line}"
                ),
            },
        })
    elements.append({"tag": "hr"})

    btn_url = f"{frontend_base_url.rstrip('/')}/crashguard/jobs"
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📊 查看任务监控"},
            "type": "primary",
            "url": btn_url,
        }],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": title_text},
        },
        "elements": elements,
    }
