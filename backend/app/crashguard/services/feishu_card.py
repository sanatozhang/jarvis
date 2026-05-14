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
    #   ① 有单 issue 异常（anomaly_total > 0）→ "其他 N 个 issue 量级在基线范围内，无需立刻动"
    #   ② 无单 issue 异常但平台级 fatal 红/黄（severity != green）
    #      → "单 issue 无突增，平台级 fatal 波动看下方分平台明细"（避免与 TL;DR 红头矛盾）
    #   ③ 无单 issue 异常且平台 green → "全平台 fatal 平稳"
    if anomaly_total > 0 and other_count > 0:
        line3 = f"> 其他 **{other_count}** 个 issue 量级在基线范围内，无需立刻动。"
    elif anomaly_total == 0 and severity in ("red", "yellow"):
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
    EXPANDED_KEYWORDS = ("关注", "新增", "突增", "TL;DR")

    for sec in sections:
        title = sec["title"]
        content = sec["content"]
        if not title and not content:
            continue
        if not title:
            # 无标题段（首段 intro）—— 跳过，已被 TL;DR 替代
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

    elements: List[Dict[str, Any]] = []
    summary_md = (
        f"**Σ** 10 分钟窗口 · 触发 **{len(items)}** 平台  ·  "
        f"阈值 ±{threshold_pp:.2f} pp（vs 前 1h 加权均值）"
    )
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": summary_md}})
    elements.append({"tag": "hr"})

    for it in items:
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
