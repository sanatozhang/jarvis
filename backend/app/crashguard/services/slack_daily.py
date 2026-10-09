"""Crashguard 早晚报的 Slack 渲染（不走 `feishu_to_slack.compile_card`）。

飞书卡片靠 `collapsible_panel` 把 FYI 段折起来，编译到 Slack 只能变成 thread
回复——实测一份早报会挂 6 条 reply，每条都是长列表，手机上几乎没法看。所以
Slack 版单独排版，只保留一屏能读完的东西，其余收进一个跳 Web 端的链接：

| 模块 | Slack | 说明 |
|---|---|---|
| 总判断（headline） | 主消息首行 | 去掉"建议工程师跟进"这类话术，只留数字 |
| 今日重点 + 必看 | 主消息 | 必看只在有异常 issue 时出现 |
| Crash-free 详表 | 主消息，原生两列 | 大盘 / 主要版本 / 新版分开列，各自 crash-free + 崩溃用户；大盘带 fatal 同比 |
| 数据口径、昨日承诺、4.0 Native、卡顿、关注点 Top3、分平台明细、核心指标详表 | 不发 | 全部在「查看完整早报」链接里 |

**不发 thread**：没有 fold，消息就是完整的一条。飞书卡片不受影响。

**Slack 一律英文**（2026-10-09）：本文件的字面量全是英文；消费的上游数据里凡是
中文的（`payload["headline"]`、coreguard 的 `headline_hint`）都**不直接用**——
headline 在这里按 tldr 数字重拼英文兜底，coreguard 只读 `headline_hint_en`，
没有就整段不带，绝不回落中文那份。飞书卡片仍是中文，不受影响。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.im.base import Rendered
from app.services.im.mrkdwn import context, divider, lark_md_to_mrkdwn, section, two_column

_COLOR_BY_SEVERITY = {"red": "#E01E5A", "yellow": "#ECB22E", "green": "#2EB67D"}
# 严重度未知时也要有色条：没有色条 Slack 不显示 text，标题就没了
_COLOR_NEUTRAL = "#868686"
_SEV_WORD = {"red": "🔴 **Critical**", "yellow": "🟡 **Watch**", "green": "✅ **Stable**"}
_SEV_UNKNOWN = "⚪ **Baseline pending**"
_STATUS_TAG = {"red": " 🔴", "yellow": " 🟡"}


def _pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else ''}{v:.0f}%"


def _fatal_thresholds() -> tuple[int, int]:
    try:
        from app.crashguard.config import get_crashguard_settings
        s = get_crashguard_settings()
        return (int(getattr(s, "daily_attention_min_events", 100) or 100),
                int(getattr(s, "daily_baseline_min_events_for_pct", 500) or 500))
    except Exception:
        return 100, 500


def _headline(tldr: Dict[str, Any], payload: Dict[str, Any], severity: str) -> str:
    sev = _SEV_WORD.get(severity, _SEV_UNKNOWN)
    total = int(tldr.get("total_users") or 0)
    crashed = int(tldr.get("crashed_users") or 0)
    if total <= 0:
        # 没有 user 维度（Datadog user 拉取失败）。compose 那边的 `payload["headline"]`
        # 是中文，Slack 不能用——按 events 单维度拼英文兜底（同 compose 的回落口径）
        today_fatal = tldr.get("fatal_today_total")
        if today_fatal is None:
            return sev
        line = f"{sev}  All-platform fatal events **{int(today_fatal):,}**"
        base_fatal = int(tldr.get("fatal_baseline_total") or 0)
        if base_fatal > 0:
            line += f" ({_pct((int(today_fatal) - base_fatal) / base_fatal * 100.0)} vs last week)"
        return line + " · user-level data unavailable"
    rate = (1.0 - crashed / total) * 100.0
    line = f"{sev}  All-platform fatal crash-free **{rate:.2f}%**"
    base_total = int(tldr.get("base_total_users") or 0)
    if base_total > 0:
        base_rate = (1.0 - int(tldr.get("base_crashed_users") or 0) / base_total) * 100.0
        pp = rate - base_rate
        line += f" ({'+' if pp >= 0 else ''}{pp:.1f}pp vs last week)"
    return line + f" · {crashed:,} users affected"


def _focus_lines(tldr: Dict[str, Any]) -> List[str]:
    chips: List[str] = []
    for p in tldr.get("platforms") or []:
        label = p.get("platform_label") or "?"
        users = p.get("crash_users")
        if users is None:
            continue
        chip = f"{label} {int(users):,} users"
        if int(users) > 0:
            chip += f" ({_pct(p.get('user_delta_pct'))})"
        if int(p.get("new_count") or 0) > 0:
            chip += f" 🆕{int(p['new_count'])}"
        chip += _STATUS_TAG.get(p.get("status") or "", "")
        chips.append(chip)
    lines: List[str] = []
    if chips:
        lines.append("**Today**  " + " · ".join(chips))

    must = tldr.get("must_see")
    if must:
        title = must.get("title") or must.get("issue_id") or ""
        url = must.get("url") or ""
        link = f"[{title}]({url})" if url else title
        if must.get("is_new"):
            extra = "new in latest build"
        elif must.get("delta_pct") is not None:
            extra = f"{_pct(must['delta_pct'])} vs last week"
        else:
            extra = ""
        plat = must.get("platform") or ""
        parts = [f"👉 **Must-see**  {f'{plat} ' if plat else ''}{link}",
                 f"{int(must.get('events') or 0):,} events"]
        if extra:
            parts.append(extra)
        lines.append(" · ".join(parts))
    return lines


def _tier_lines(stats: Dict[str, Any]) -> List[str]:
    """一个口径（大盘 / 主要版本 / 新版）的 crash-free + 崩溃用户两行。"""
    out: List[str] = []
    cf = stats.get("crash_free_users_pct")
    if cf is None:
        cf = stats.get("crash_free_pct")
    if cf is not None:
        out.append(f"crash-free  **{float(cf):.2f}%**")
    if int(stats.get("total_users") or 0) > 0:
        out.append(f"Crashed users  {int(stats.get('crashed_users') or 0):,} / {int(stats['total_users']):,}")
    return out


def _crash_free_column(label: str, all_stats: Optional[Dict[str, Any]],
                       main_stats: Optional[Dict[str, Any]],
                       latest_stats: Optional[Dict[str, Any]],
                       wow: Optional[Dict[str, Any]]) -> str:
    """单平台一栏：大盘 / 主要版本 / 新版 三个口径分开列，各自 crash-free + 崩溃用户。"""
    lines = [f"**{label}**"]
    if all_stats:
        lines.append("__Overall__")
        lines.extend(_tier_lines(all_stats))
        if wow:
            min_today, min_base = _fatal_thresholds()
            today = int(wow.get("today_fatal") or 0)
            if today < min_today or int(wow.get("baseline_fatal") or 0) < min_base:
                cmp = "low volume"
            else:
                cmp = f"{_pct(wow.get('fatal_delta_pct'))} vs last week"
            lines.append(f"fatal  {today:,} ({cmp})")
    if main_stats and main_stats.get("version"):
        share = main_stats.get("share_of_platform_pct")
        share_str = f" ({float(share):.0f}% of users)" if share is not None else ""
        lines.append(f"__Primary version__ `{main_stats['version']}`{share_str}")
        lines.extend(_tier_lines(main_stats))
    # 新版跟主要版本是同一个 build 时不重复列
    if latest_stats and latest_stats.get("version") and (
            not main_stats or latest_stats.get("version") != main_stats.get("version")):
        lines.append(f"__Latest version__ `{latest_stats['version']}`")
        lines.extend(_tier_lines(latest_stats))
    return "\n".join(lines)


def _crash_free_block(payload: Dict[str, Any]) -> Optional[dict]:
    detail = payload.get("crash_free_detail") or {}
    all_p = (detail.get("all_versions") or {}).get("platforms") or {}
    if not all_p:
        return None
    main_p = (detail.get("top_user_versions") or {}).get("platforms") or {}
    latest_p = (detail.get("latest_versions") or {}).get("platforms") or {}
    wow_p = (payload.get("dual_window") or {}).get("platforms") or {}
    cols = [
        _crash_free_column(label, all_p.get(key), main_p.get(key), latest_p.get(key), wow_p.get(key))
        for key, label in (("IOS", "🍎 iOS"), ("ANDROID", "📱 Android"))
    ]
    return two_column(*(lark_md_to_mrkdwn(c) for c in cols))


def build_daily_slack(
    *,
    report_type: str,
    target_date: str,
    payload: Dict[str, Any],
    frontend_base_url: str,
    coreguard_section: Optional[Dict[str, Any]] = None,
) -> Rendered:
    is_morning = report_type == "morning"
    tldr = payload.get("tldr") or {}
    has_anomaly = sum(int(payload.get(k) or 0)
                      for k in ("new_count", "surge_count", "regression_count")) > 0
    severity = tldr.get("severity") or ("red" if has_anomaly else "green")

    title = (f"🌅 [Core Metrics] Daily recap · {target_date}" if is_morning
             else f"🌇 [Core Metrics] Evening flash · {target_date}")

    headline = _headline(tldr, payload, severity)
    # 只认英文版 hint；coreguard 没给就不带（绝不回落中文的 headline_hint）
    cg = coreguard_section or {}
    hint_en = cg.get("headline_hint_en") if cg.get("available") else None
    if hint_en:
        headline = f"{hint_en}; {headline}"

    # 各段先按飞书 lark_md 写，统一过一遍转换：链接里的方括号、紧贴汉字的加粗
    # 这些 Slack 坑都在 lark_md_to_mrkdwn 里处理过了
    blocks: List[dict] = [section(lark_md_to_mrkdwn(headline))]
    focus = _focus_lines(tldr)
    if focus:
        blocks.append(section(lark_md_to_mrkdwn("\n".join(focus))))
    cf_block = _crash_free_block(payload)
    if cf_block:
        blocks.append(divider())
        blocks.append(cf_block)

    report_url = (f"{frontend_base_url.rstrip('/')}/crashguard/reports"
                  f"?type={report_type}&date={target_date}")
    blocks.append(context(
        f"<{report_url}|View full {'report' if is_morning else 'evening flash'} →>"
        "  4.0 Native · Top 3 focus issues · Per-platform details"
    ))

    return Rendered(
        payload=blocks,
        # 带色条时 Slack 把 text 显示在 attachment 上方，所以标题只放这里、
        # blocks 里不再放 header，否则标题会出现两次。
        text=f"*{title}*",
        color=_COLOR_BY_SEVERITY.get(severity, _COLOR_NEUTRAL),
    )
