"""Coreguard 聚合摘要卡 v2 — 只列异常 + 顶部一句话总结 + dashboard 链接。

设计原则：
  - 一眼看出问题：标题 + headline 一句话总结
  - 正常项不展示：减少视觉噪声
  - 异常项突出：颜色 + 箭头 + 涨跌方向 + 阈值对比
  - dashboard 直链：可立即跳转 Datadog 查看
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("coreguard.feishu_summary")


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _fmt_value(value_type: str, v: Optional[float]) -> str:
    if v is None:
        return "—"
    if value_type == "percent_pp":
        return f"{v:.3f}%"
    if value_type == "latency_pct":
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


def _direction_word(direction: str, change: Optional[float]) -> str:
    """生成「上涨/下降」自然语言。"""
    if change is None:
        return "无数据"
    if change > 0:
        return "上涨"
    if change < 0:
        return "下降"
    return "持平"


def _bad_emoji(direction: str, change: Optional[float]) -> str:
    """异常方向标记。"""
    if change is None:
        return "⚪"
    bad_up = direction == "up_is_bad" and change > 0
    bad_down = direction == "down_is_bad" and change < 0
    if bad_up:
        return "🔺"
    if bad_down:
        return "🔻"
    return ""


# ---------------------------------------------------------------------------
# Headline (一句话总结)
# ---------------------------------------------------------------------------

def _headline(breached: List[Dict[str, Any]]) -> str:
    if not breached:
        return "本小时所有核心指标正常"

    # 按 tier 分组
    p0 = [r for r in breached if r["tier"] == "P0"]
    p1 = [r for r in breached if r["tier"] == "P1"]

    # 找最严重的一个（按 change 绝对幅度）
    def _severity(r):
        c = r.get("change")
        if c is None:
            return 0
        return abs(c)

    worst = max(breached, key=_severity)
    direction_word = _direction_word(worst["direction"], worst["change"])
    change_str = _fmt_change(worst["value_type"], worst["change"])

    parts = []
    if p0:
        parts.append(f"{len(p0)} 项 P0 核心指标异常")
    if p1:
        parts.append(f"{len(p1)} 项 P1 性能指标异常")
    summary = "、".join(parts)

    return (
        f"{summary}：**{worst['title']}** {direction_word} `{change_str}` "
        f"（vs 上周同时段），需立即跟进。"
    )


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------

def _breached_block(r: Dict[str, Any]) -> str:
    """单条异常的展示块（lark_md）。"""
    tier = r["tier"]
    title = r["title"]
    cur = _fmt_value(r["value_type"], r["current_value"])
    base = _fmt_value(r["value_type"], r["baseline_value"])
    chg = _fmt_change(r["value_type"], r["change"])
    th = _fmt_threshold(r["value_type"], r["threshold"])
    direction_word = _direction_word(r["direction"], r["change"])
    emoji = _bad_emoji(r["direction"], r["change"])

    return (
        f"**[{tier}] {title}** {emoji}\n"
        f"　{direction_word} `{chg}` (阈值 {th})\n"
        f"　当前 `{cur}` · 上周 `{base}`"
    )


def build_summary_card(
    cur_start, cur_end, base_start, base_end,
    breached: List[Dict[str, Any]],
    healthy: List[Dict[str, Any]],   # 仍接收以保持签名，但不展示
    errored: List[Dict[str, Any]],
    forced: bool,
    dashboard_id: str,
    datadog_site: str,
) -> Dict[str, Any]:
    n_breach = len(breached)
    n_healthy = len(healthy)
    n_err = len(errored)
    total = n_breach + n_healthy + n_err

    # Header 颜色 + 标题
    if n_breach > 0:
        template = "red"
        title = f"[coreguard] ⚠️ 核心指标异常告警 ({n_breach}/{total})"
    elif forced:
        template = "blue"
        title = f"[coreguard] 🧪 演示 — {total} 项全部正常"
    else:
        template = "green"
        title = f"[coreguard] ✅ {total} 项核心指标全部正常"

    elements: List[Dict[str, Any]] = []

    # 顶部一句话 headline
    headline_text = _headline(breached)
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"📢 {headline_text}"},
    })

    # 窗口对比信息（小字）
    # cur_start/cur_end 是 datetime.utcnow() 返回的 naive UTC datetime；
    # naive .timestamp() 默认按本地时区解释会偏 8h，必须显式打上 UTC tzinfo。
    from datetime import timezone as _tz
    from_ts = int(cur_start.replace(tzinfo=_tz.utc).timestamp() * 1000)
    to_ts = int(cur_end.replace(tzinfo=_tz.utc).timestamp() * 1000)
    dashboard_url = (
        f"https://app.{datadog_site}/dashboard/{dashboard_id}"
        f"?from_ts={from_ts}&to_ts={to_ts}&live=false"
    )
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "lark_md",
            "content": (
                f"当前 {cur_start.strftime('%m-%d %H:%M')} ~ {cur_end.strftime('%H:%M')} UTC"
                f"  ·  上周 {base_start.strftime('%m-%d %H:%M')} ~ {base_end.strftime('%H:%M')} UTC"
                f"  ·  共评估 {total} 项 (异常 {n_breach}{('，缺数据 '+str(n_err)) if n_err else ''})"
            ),
        }],
    })

    # 异常列表（核心区）
    if breached:
        elements.append({"tag": "hr"})
        # P0 在前
        breached_sorted = sorted(
            breached,
            key=lambda x: (0 if x["tier"] == "P0" else 1, -(abs(x.get("change") or 0))),
        )
        for r in breached_sorted:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": _breached_block(r)},
            })

    # 缺数据兜底（仅当真有 errored 时）
    if errored and n_err > 0:
        elements.append({"tag": "hr"})
        names = "、".join(r["title"] for r in errored[:5])
        if n_err > 5:
            names += f" 等 {n_err} 项"
        elements.append({
            "tag": "note",
            "elements": [{"tag": "lark_md", "content": f"⚪ 缺数据：{names}"}],
        })

    # Footer — dashboard 链接（按钮形式更显眼）
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📊 打开 Datadog Dashboard 排查"},
            "type": "primary",
            "url": dashboard_url,
        }],
    })

    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


# ---------------------------------------------------------------------------
# Sender — email 优先（演示阶段不打扰群）
# ---------------------------------------------------------------------------

async def send(card: Dict[str, Any]) -> bool:
    from app.coreguard.config import get_coreguard_settings
    s = get_coreguard_settings()
    if not s.feishu_enabled:
        logger.info("feishu_enabled=false, skip send")
        return False
    if not s.feishu_target_chat_id and not s.feishu_target_email:
        logger.warning("no feishu target configured")
        return False

    # 优先级：prefer_email=true → email first；否则 chat_id first
    use_email = s.feishu_prefer_email and bool(s.feishu_target_email)

    try:
        from app.services.feishu_cli import send_interactive_card
        if use_email:
            logger.info("coreguard send via email=%s (demo private)", s.feishu_target_email)
            return await send_interactive_card(email=s.feishu_target_email, card=card)
        if s.feishu_target_chat_id:
            return await send_interactive_card(chat_id=s.feishu_target_chat_id, card=card)
        return await send_interactive_card(email=s.feishu_target_email, card=card)
    except Exception as e:
        logger.error("feishu send_interactive_card failed: %s", e)
        return False
