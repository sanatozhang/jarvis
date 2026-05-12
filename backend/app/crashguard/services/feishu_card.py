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
    title_emoji = "🌅" if is_morning else "🌇"
    title_text = f"{title_emoji} Crashguard {'早报' if is_morning else '晚报'}  {target_date}"

    elements: List[Dict[str, Any]] = []

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
    hour_utc: datetime,
    new_items: List[Dict[str, Any]],
    surge_items: List[Dict[str, Any]],
    threshold_pct: float = 10.0,
    frontend_base_url: str = "http://localhost:3000",
    alert_id: int | None = None,
) -> Dict[str, Any]:
    """构造 hourly 告警 interactive card payload。

    复用早晚报色板：异常 → red header，平稳 → 不该走到这里。
    聚合 digest 一张卡，避免高频刷屏。
    严格不含 PR 修复内容——按用户要求，PR 状态查看走前端。
    """
    new_n = len(new_items or [])
    surge_n = len(surge_items or [])
    # 显示用新加坡时区（UTC+8）—— Plaud 主用户群体所在时区
    from datetime import timedelta as _td
    sg_dt = hour_utc + _td(hours=8)
    hour_label = sg_dt.strftime("%Y-%m-%d %H:%M SGT")
    template = "red"  # 触发到这里必有异常
    title_text = f"🚨 Crashguard 实时告警 · {hour_label}"

    elements: List[Dict[str, Any]] = []

    # 顶部摘要
    summary_md = (
        f"**Σ** 过去 3 小时 · 新增 **{new_n}** · 上涨 **{surge_n}**  ·  "
        f"阈值 +{threshold_pct:.0f}%（对比上周同 3h 块，SHoW-3h）"
    )
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": summary_md},
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
            surge_lines.append(
                f"- {pe} [{it.get('title') or it['issue_id']}]({url})  ·  "
                f"**{it['events_h']}** vs {it['baseline']:.0f} ({src})  ·  "
                f"**+{it['growth_pct']:.1f}%** ⬆️{sess_str}"
            )
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(surge_lines)},
        })
        elements.append({"tag": "hr"})

    # 底部按钮：直接跳 reports 页对应告警详情（带 alert_id 自动打开 modal）
    if alert_id is not None:
        btn_url = (
            f"{frontend_base_url.rstrip('/')}/crashguard/reports"
            f"?type=hourly_alert&alert_id={alert_id}"
        )
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
