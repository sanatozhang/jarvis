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


def _is_band(r: Dict[str, Any]) -> bool:
    return (r.get("baseline_mode") == "band") and r.get("band_lower") is not None


def _sigma_level(dist: Optional[float]) -> tuple[str, str]:
    """穿出 σ 数 → (emoji, 级别词)。design §5.1。"""
    d = abs(dist or 0)
    if d >= 6:
        return "🔴", "紧急"
    if d >= 4:
        return "🟠", "警告"
    return "🟡", "关注"


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

    parts = []
    if p0:
        parts.append(f"{len(p0)} 项 P0 核心指标异常")
    if p1:
        parts.append(f"{len(p1)} 项 P1 性能指标异常")
    summary = "、".join(parts)

    if _is_band(worst):
        emoji, level = _sigma_level(worst.get("change"))
        return (
            f"{summary}：{emoji} **{worst['title']}** 偏离预测带 "
            f"`{abs(worst.get('change') or 0):.1f}σ`（{level}，基线=近期同时段），需立即跟进。"
        )
    # 旧/absolute 口径
    direction_word = _direction_word(worst["direction"], worst["change"])
    change_str = _fmt_change(worst["value_type"], worst["change"])
    return (
        f"{summary}：**{worst['title']}** {direction_word} `{change_str}` "
        f"（vs 上周同时段），需立即跟进。"
    )


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------

def _build_dashboard_url(
    dashboard_id: str, datadog_site: str,
    start_ms: int, end_ms: int,
    widget_id: Optional[int] = None,
) -> str:
    """构造时间范围深链。若有 datadog_widget_id → 加 fullscreen_widget 直接打开 tile。"""
    base = (
        f"https://app.{datadog_site}/dashboard/{dashboard_id}"
        f"?from_ts={start_ms}&to_ts={end_ms}&live=false"
    )
    if widget_id is not None:
        base += f"&fullscreen_widget={widget_id}"
    return base


def _breached_block(
    r: Dict[str, Any],
    cur_start_ms: int, cur_end_ms: int,
    base_start_ms: int, base_end_ms: int,
    dashboard_id: str, datadog_site: str,
) -> str:
    """单条异常的展示块（lark_md）— 当前值 / 上周值各挂一条 Datadog 深链。"""
    tier = r["tier"]
    title = r["title"]
    vt = r["value_type"]
    cur = _fmt_value(vt, r["current_value"])
    widget_id = r.get("datadog_widget_id")
    cur_url = _build_dashboard_url(dashboard_id, datadog_site, cur_start_ms, cur_end_ms, widget_id)

    # 带引擎：展示 穿出σ + 预测μ + 正常带
    if _is_band(r):
        emoji, level = _sigma_level(r.get("change"))
        mu = _fmt_value(vt, r.get("baseline_value"))
        lo = _fmt_value(vt, r.get("band_lower"))
        hi = _fmt_value(vt, r.get("band_upper"))
        n = r.get("baseline_n")
        return (
            f"**[{tier}] {title}** {emoji}\n"
            f"　偏离预测带 `{abs(r.get('change') or 0):.1f}σ`（{level}）\n"
            f"　当前 [`{cur}`]({cur_url}) · 预测 μ`{mu}` · 正常带 `[{lo}, {hi}]`"
            f"{f'（基线 {n} 点）' if n else ''}"
        )

    # 旧/absolute 口径：上周同时段 + 阈值
    base = _fmt_value(vt, r["baseline_value"])
    chg = _fmt_change(vt, r["change"])
    th = _fmt_threshold(vt, r["threshold"]) if r.get("threshold") else "—"
    direction_word = _direction_word(r["direction"], r["change"])
    emoji = _bad_emoji(r["direction"], r["change"])
    base_url = _build_dashboard_url(dashboard_id, datadog_site, base_start_ms, base_end_ms, widget_id)
    return (
        f"**[{tier}] {title}** {emoji}\n"
        f"　{direction_word} `{chg}` (阈值 {th})\n"
        f"　当前 [`{cur}`]({cur_url}) · 上周 [`{base}`]({base_url})"
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
    def _utc_ms(dt):
        return int(dt.replace(tzinfo=_tz.utc).timestamp() * 1000)
    cur_start_ms = _utc_ms(cur_start)
    cur_end_ms = _utc_ms(cur_end)
    base_start_ms = _utc_ms(base_start)
    base_end_ms = _utc_ms(base_end)
    dashboard_url = (
        f"https://app.{datadog_site}/dashboard/{dashboard_id}"
        f"?from_ts={cur_start_ms}&to_ts={cur_end_ms}&live=false"
    )
    _baseline_days = max((cur_start - base_start).days, 1)
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "lark_md",
            "content": (
                f"当前窗口 {cur_start.strftime('%m-%d %H:%M')} ~ {cur_end.strftime('%H:%M')} UTC"
                f"  ·  基线 近 {_baseline_days} 天同时段（预测带 median±k·MAD）"
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
                "text": {"tag": "lark_md", "content": _breached_block(
                    r,
                    cur_start_ms, cur_end_ms,
                    base_start_ms, base_end_ms,
                    dashboard_id, datadog_site,
                )},
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

def _today_local_date():
    """配额按 Asia/Shanghai 自然日切（容器 TZ=Asia/Shanghai → datetime.now 即可）。"""
    from datetime import date as _date_t
    return _date_t.today()


def _extract_card_title(card: Dict[str, Any]) -> str:
    try:
        return ((card.get("header") or {}).get("title") or {}).get("content", "") or ""
    except Exception:
        return ""


async def _count_group_sent_today(today) -> int:
    """今日已成功发到群的告警数（用于配额判定）。"""
    from sqlalchemy import select, func
    from app.coreguard.models import CoreguardAlertDispatch
    from app.db.database import get_session
    async with get_session() as session:
        n = (await session.execute(
            select(func.count(CoreguardAlertDispatch.id)).where(
                CoreguardAlertDispatch.sent_date == today,
                CoreguardAlertDispatch.target_kind == "group",
                CoreguardAlertDispatch.sent_ok.is_(True),
            )
        )).scalar_one()
    return int(n or 0)


async def _record_dispatch(
    *, today, target_kind: str, target_value: str, sent_ok: bool,
    alert_title: str, breach_count: int, overflow_from_group: bool,
) -> None:
    from app.coreguard.models import CoreguardAlertDispatch
    from app.db.database import get_session
    async with get_session() as session:
        row = CoreguardAlertDispatch(
            sent_date=today,
            target_kind=target_kind,
            target_value=(target_value or "")[:128],
            sent_ok=sent_ok,
            alert_title=(alert_title or "")[:256],
            breach_count=int(breach_count or 0),
            overflow_from_group=overflow_from_group,
        )
        session.add(row)
        await session.commit()


async def send(card: Dict[str, Any], breach_count: int = 0) -> bool:
    """路由规则（2026-05-28 新）：
       1) 有群 + 今日群配额未满 → 发群（target_kind='group'）
       2) 群配额已满 或 无群 → 发 overflow_email（target_kind='email', overflow_from_group=True）
       3) 两者都没配置 → skip
    每次成功/失败均写入 coreguard_alert_dispatches 用于审计 + 配额判定。
    """
    from app.coreguard.config import get_coreguard_settings
    s = get_coreguard_settings()
    if not s.feishu_enabled:
        logger.info("feishu_enabled=false, skip send")
        return False
    if not s.feishu_target_chat_id and not s.feishu_overflow_email and not s.feishu_target_email:
        logger.warning("no feishu target configured (chat_id/overflow_email/target_email 全空)")
        return False

    today = _today_local_date()
    title = _extract_card_title(card)
    quota = int(s.feishu_group_daily_quota or 0)
    sent_today = await _count_group_sent_today(today) if s.feishu_target_chat_id else 0

    try:
        from app.services.feishu_cli import send_interactive_card

        # 1) 优先群（配额内）
        if s.feishu_target_chat_id and sent_today < quota:
            ok = await send_interactive_card(chat_id=s.feishu_target_chat_id, card=card)
            logger.info(
                "coreguard send → group %s (today %d/%d, ok=%s)",
                s.feishu_target_chat_id, sent_today + (1 if ok else 0), quota, ok,
            )
            await _record_dispatch(
                today=today, target_kind="group",
                target_value=s.feishu_target_chat_id, sent_ok=bool(ok),
                alert_title=title, breach_count=breach_count, overflow_from_group=False,
            )
            return bool(ok)

        # 2) 群配额已满 / 无群 → 转个人
        overflow = s.feishu_overflow_email or s.feishu_target_email
        if overflow:
            ok = await send_interactive_card(email=overflow, card=card)
            logger.info(
                "coreguard send → overflow email %s (group_sent_today=%d quota=%d, ok=%s)",
                overflow, sent_today, quota, ok,
            )
            await _record_dispatch(
                today=today, target_kind="email",
                target_value=overflow, sent_ok=bool(ok),
                alert_title=title, breach_count=breach_count,
                overflow_from_group=(sent_today >= quota and bool(s.feishu_target_chat_id)),
            )
            return bool(ok)

        logger.warning("group quota exhausted but no overflow_email configured, dropping alert")
        return False
    except Exception as e:
        logger.error("feishu send_interactive_card failed: %s", e)
        return False
