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


def build_daily_card(
    report_type: str,
    target_date: str,
    markdown: str,
    payload: Dict[str, Any],
    frontend_base_url: str = "http://localhost:3000",
) -> Dict[str, Any]:
    """构造飞书 interactive card payload。"""
    is_morning = report_type == "morning"
    new_count = int(payload.get("new_count") or 0)
    surge_count = int(payload.get("surge_count") or 0)
    drop_count = int(payload.get("regression_count") or 0)
    has_anomaly = (new_count + surge_count + drop_count) > 0

    # 卡片头部颜色：异常用 red，平稳用 turquoise
    template = "red" if has_anomaly else "turquoise"
    # 早晚报差异化（A+B 方案）：
    #   早报 = "Crashguard 日报"（昨日 24h 总览）
    #   晚报 = "Crashguard 速报"（日内增量 vs 上周同段）—— 两字对仗，明确区分
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

    elements: List[Dict[str, Any]] = []

    # 数据口径 banner（顶部置顶，让群里人 2 秒识别本卡片是日报还是速报）
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": scope_md},
    })

    # 顶部摘要小标签
    summary_md = (
        f"**Σ** 新增 **{new_count}** · 突增 **{surge_count}** · 下降 **{drop_count}**"
        if has_anomaly
        else "🌿 **数据平稳，安全无虞**"
    )
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": summary_md},
    })
    elements.append({"tag": "hr"})

    # 切段插入
    sections = _split_sections(markdown)
    for sec in sections:
        # skip 空段
        if not sec["title"] and not sec["content"]:
            continue
        # 段标题（已含 emoji）
        if sec["title"]:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{sec['title']}**"},
            })
        if sec["content"]:
            # 卡片单 lark_md 长度限制 ~4000 char，超长截断
            content = sec["content"]
            if len(content) > 3500:
                content = content[:3500] + "\n\n_…内容过长，已截断，详见 Web 端_"
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            })
        elements.append({"tag": "hr"})

    # 底部按钮：跳 Web 端
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📊 在 Web 端查看 / 操作"},
                "type": "primary",
                "url": f"{frontend_base_url.rstrip('/')}/crashguard",
            },
        ],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title_text},
        },
        "elements": elements,
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
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📊 在 Web 端查看"},
                "type": "primary",
                "url": btn_url,
            },
        ],
    })

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
