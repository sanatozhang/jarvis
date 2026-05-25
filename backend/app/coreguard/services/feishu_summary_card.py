"""Coreguard 聚合摘要卡：一次跑发一张卡，列所有超阈指标 + 正常摘要 + 异常数据。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("coreguard.feishu_summary")


def _fmt_value(value_type: str, v: Optional[float]) -> str:
    if v is None:
        return "—"
    if value_type == "percent_pp":
        return f"{v:.3f}%"
    if value_type == "latency_pct":
        # 假设单位是 ms；统一保留 1 位
        return f"{v:.2f}"
    return f"{v:.2f}"


def _fmt_change(value_type: str, change: Optional[float]) -> str:
    if change is None:
        return "—"
    if value_type == "percent_pp":
        sign = "+" if change >= 0 else ""
        return f"{sign}{change:.3f} pp"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change * 100:.2f}%"


def _fmt_threshold(value_type: str, threshold: Dict[str, float]) -> str:
    if "pp" in threshold:
        return f"±{threshold['pp']} pp"
    return f"±{threshold.get('pct', 0)*100:.0f}%"


def _arrow(direction: str, change: Optional[float]) -> str:
    if change is None:
        return ""
    bad_down = (direction == "down_is_bad" and change < 0)
    bad_up = (direction == "up_is_bad" and change > 0)
    if bad_down or bad_up:
        return "🔻" if change < 0 else "🔺"
    return "↘" if change < 0 else "↗"


def _breached_row(r: Dict[str, Any]) -> str:
    tier = r["tier"]
    title = r["title"]
    cur = _fmt_value(r["value_type"], r["current_value"])
    base = _fmt_value(r["value_type"], r["baseline_value"])
    chg = _fmt_change(r["value_type"], r["change"])
    arrow = _arrow(r["direction"], r["change"])
    th = _fmt_threshold(r["value_type"], r["threshold"])
    return f"**[{tier}] {title}** {arrow}\n　当前 `{cur}` ｜ 上周 `{base}` ｜ 变化 `{chg}` (阈值 `{th}`)"


def _healthy_top(rs: List[Dict[str, Any]], top: int = 5) -> str:
    """正常项摘要：列前 top 个，剩余折叠成 `... +N more`。"""
    if not rs:
        return "—"
    lines = []
    for r in rs[:top]:
        cur = _fmt_value(r["value_type"], r["current_value"])
        chg = _fmt_change(r["value_type"], r["change"])
        lines.append(f"・{r['title']} `{cur}` ({chg})")
    if len(rs) > top:
        lines.append(f"・…还有 {len(rs) - top} 项正常")
    return "\n".join(lines)


def build_summary_card(
    cur_start, cur_end, base_start, base_end,
    breached: List[Dict[str, Any]],
    healthy: List[Dict[str, Any]],
    errored: List[Dict[str, Any]],
    forced: bool,
    dashboard_id: str,
    datadog_site: str,
) -> Dict[str, Any]:
    n_breach = len(breached)
    n_healthy = len(healthy)
    n_err = len(errored)
    total = n_breach + n_healthy + n_err

    # Header
    if n_breach > 0:
        template = "red"
        headline = f"⚠️ {n_breach} 项异常 / 共 {total} 项"
    elif forced:
        template = "blue"
        headline = f"🧪 强制演示 — {total} 项全部正常"
    else:
        template = "green"
        headline = f"✅ {n_healthy} 项全部正常"

    title = f"[coreguard·核心指标] {headline}"

    elements: List[Dict[str, Any]] = []

    # 窗口信息
    window_label = (
        f"**当前窗口** {cur_start.strftime('%Y-%m-%d %H:%M')} ~ {cur_end.strftime('%H:%M')} UTC\n"
        f"**上周同时段** {base_start.strftime('%Y-%m-%d %H:%M')} ~ {base_end.strftime('%H:%M')} UTC"
    )
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": window_label}})
    elements.append({"tag": "hr"})

    # 异常列表
    if breached:
        # P0 在前，P1 在后
        breached_sorted = sorted(breached, key=lambda x: (0 if x["tier"] == "P0" else 1, x["title"]))
        body = "\n\n".join(_breached_row(r) for r in breached_sorted)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"### 🔴 异常指标\n\n{body}"}})
        elements.append({"tag": "hr"})

    # 正常项摘要
    if healthy:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"### 🟢 正常项 ({n_healthy})\n{_healthy_top(healthy, top=5)}"}})

    # 异常/缺数据
    if errored:
        err_lines = "\n".join(f"・{r['title']} — {r.get('error') or 'no data'}" for r in errored[:5])
        if len(errored) > 5:
            err_lines += f"\n・…还有 {len(errored) - 5} 项"
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"### ⚪ 缺数据/异常 ({n_err})\n{err_lines}"}})

    # Footer
    from_ts = int(cur_start.timestamp() * 1000)
    to_ts = int(cur_end.timestamp() * 1000)
    dashboard_url = f"https://app.{datadog_site}/dashboard/{dashboard_id}?from_ts={from_ts}&to_ts={to_ts}&live=false"
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "note",
        "elements": [{"tag": "lark_md",
                      "content": f"📊 [打开 Datadog Dashboard]({dashboard_url})  ·  Demo 阶段（无 Ack / 升级，正式版扩展）"}]
    })

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


async def send(card: Dict[str, Any]) -> bool:
    from app.coreguard.config import get_coreguard_settings
    s = get_coreguard_settings()
    if not s.feishu_enabled:
        logger.info("feishu_enabled=false, skip send")
        return False
    if not s.feishu_target_chat_id and not s.feishu_target_email:
        logger.warning("no feishu target")
        return False
    try:
        from app.services.feishu_cli import send_interactive_card
        if s.feishu_target_chat_id:
            return await send_interactive_card(chat_id=s.feishu_target_chat_id, card=card)
        return await send_interactive_card(email=s.feishu_target_email, card=card)
    except Exception as e:
        logger.error("feishu send_interactive_card failed: %s", e)
        return False
