"""Graygate 日报的 Slack Block Kit 渲染。

消费 `card_builder.collect_report_data()` 的同一份数据（取数不重跑，见那边
`GraygateReportData` 的注释）。

## 主消息放什么、thread 放什么

graygate 的飞书卡片里**没有** `collapsible_panel`（全仓那 9 处折叠都在
crashguard），所以这里的划分是重新定的，判据是「这条消息不展开也能用吗」：

| | 去处 | 理由 |
|---|---|---|
| 恶化摘要 | 主消息 | 唯一需要立刻有人动手的东西 |
| iOS/Android 指标双列 | **主消息** | 日报本身就是这张表；塞进 thread 的话，没有恶化的那天主消息会变成一条没有内容的壳 |
| 新增崩溃堆栈 | thread | 长列表，附录性质 |
| Top5 崩溃 / Top5 卡顿 | thread | 同上，而且不看是否新增，天天都有 |

这跟 crashguard 早晚报的划分会不一样（那张卡自己的 docstring 写了"把 80%
行动力压到第一屏"，它的折叠段本来就标好了哪些是 FYI）——**不要为了"统一"
把 graygate 的指标表也塞进 thread**。

## 2026-10-09：精简版 + 跳网页端

传了 `report_url`（正常发送路径都会传）时，主消息只放**核心指标**（参与恶化判定
的那几个，`metric_rows` 里 is_core=True），新增崩溃只给条数，其余（非核心指标、
新增崩溃堆栈、Top5 崩溃/卡顿）全部收进「查看完整日报 →」链接，**不再发 thread**——
跟 crashguard 早报同一个处理方式。完整版 markdown 见 `report_markdown.py`，发送时
落库缓存（`report_store.py`）。没有 `report_url`（前端地址没配）时附录退回 thread，
保证信息不丢。

**Slack 全英文**：见下方 `_METRIC_EN` 那段注释。

## 色条

飞书 card 的 `template: red/turquoise` 在 Block Kit 里没有对等物，走 legacy
attachment 的 `color`。判据复用 `GraygateReportData.is_red`，两个渠道同一套。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from app.graygate.services.card_builder import GraygateReportData, collect_report_data
from app.services.im.base import Fold, Rendered
from app.services.im.mrkdwn import (
    context,
    divider,
    lark_md_to_mrkdwn,
    section,
    two_column,
)

logger = logging.getLogger("jarvis.graygate.slack_report")

# Slack 的 attachment 色条。用十六进制而不是 "danger"/"good" 这两个语义名：
# 语义名在部分客户端上的渲染色跟飞书的 red/turquoise 差得比较远，这两个值是
# 对着飞书卡片调的。
_COLOR_RED = "#E01E5A"
_COLOR_OK = "#2EB67D"


# ---------------------------------------------------------------------------
# 2026-10-09：Slack 全英文（用户要求 Slack 消息不含中文；飞书 / 网页端仍是中文）。
# 所以 Slack 不复用 columns_md / worsen_lines / *_md 这些拼好的中文片段，而是从
# 结构化字段（tiers / metric_rows / worsen_items）重新渲染。
# ---------------------------------------------------------------------------
_PLATFORM_EN = {"ios": "🍎 iOS", "android": "🤖 Android"}
_TIER_EN = {"大盘": "Overall", "主要版本": "Primary"}

# metrics.yaml 里的 title 是 Datadog 看板上的 widget 名，部分是中文，这里给英文展示名
_METRIC_EN = {
    "crash_free": "Crash-free sessions",
    "android_anr": "ANR rate",
    "hang_rate": "Hang rate",
    "refresh_rate": "Refresh rate",
    "fps": "Avg FPS per run",
    "jank": "Jank per session (p75/p90)",
    "cold_startup_p90": "Cold startup p90",
    "memory_usage": "Memory usage",
    "home_render": "Home list load (p75/p90)",
    "detail_render_p90": "Detail first screen p90",
    "summary_render_p90": "Detail WebView render p90",
}

# card_builder 的 sentinel 文案 → 英文
_SENTINEL_EN = {
    "—（不适用）": "— (n/a)",
    "—（无数据）": "— (no data)",
    "—（样本不足）": "— (low sample)",
    "—（取数失败）": "— (query failed)",
}


def _metric_en(key: str, fallback: str = "") -> str:
    if key in _METRIC_EN:
        return _METRIC_EN[key]
    return fallback if fallback.isascii() and fallback else key


def _cell_en(v: str) -> str:
    return _SENTINEL_EN.get(v, v)


def _title_en(data: GraygateReportData) -> str:
    return f"🆕 [4.0.3 Gray] Daily Metrics · {data.target_date.isoformat()}"


def _banner_en(data: GraygateReportData) -> str:
    return (
        f"📊 Window {data.target_date.strftime('%m-%d')} 00:00–24:00 BJT · "
        f"baseline {data.d1_day.strftime('%m-%d')} (previous workday) · "
        f"overall version pattern `{data.version_pattern}`"
    )


def _tier_head(t) -> str:
    if t.label == "大盘":
        head = f"_Overall ({t.version})_"
    elif t.version:
        head = f"_Primary_ `{t.version}`" + (" (manual)" if t.manual else "")
    else:
        head = "_Primary_ — (no data)"
    if t.sessions is not None:
        head += f" · {t.sessions:,} sessions"
    return head


def _core_column(data: GraygateReportData, platform: str) -> str:
    """一个平台的核心指标列：主要版本在前、大盘在后；「不适用」的格子不列。"""
    core = [(k, name) for k, name, is_core in data.metric_rows if is_core]
    tiers = data.tiers.get(platform) or []
    # 主要版本是这次灰度真正在看的包，排最前（2026-10-09 用户要求）
    tiers = sorted(tiers, key=lambda t: 0 if t.label == "主要版本" else 1)
    lines: List[str] = [f"*{_PLATFORM_EN[platform]}*"]
    for t in tiers:
        lines += ["", _tier_head(t)]
        for key, name in core:
            v = t.cells.get(key, "")
            if not v or "不适用" in v:
                continue
            lines.append(f"• {_metric_en(key, name)}: {_cell_en(v)}")
    return "\n".join(lines)


def _worsen_en(data: GraygateReportData) -> List[str]:
    out = []
    for w in data.worsen_items:
        out.append(
            f"• {_PLATFORM_EN.get(w.platform, w.platform)} [{_TIER_EN.get(w.tier_label, w.tier_label)}] "
            f"{_metric_en(w.key)} {w.value} {w.arrow} {w.delta} (2 workdays in a row)"
        )
    return out


def _list_body(md: Optional[str]) -> str:
    """`**中文标题**\n\n- 条目...` → 只要条目（条目本身是平台/版本/events/英文 issue 标题）。"""
    if not md:
        return ""
    _, _, body = md.partition("\n")
    return lark_md_to_mrkdwn(body.strip())


def assemble_slack_message(data: GraygateReportData, report_url: Optional[str] = None) -> Rendered:
    """`GraygateReportData` → Slack 主消息（英文）。

    标题只放在 `text` 里：带色条时 Slack 把 text 显示在 attachment 上方，blocks 里
    再放 header 就会出现两遍（2026-10-09 用户反馈）。
    """
    n_worse = len(data.worsen_items) or len(data.worsen_lines)
    blocks: List[dict] = [context(_banner_en(data))]

    if data.worsen_items:
        blocks.append(divider())
        blocks.append(section(
            "*🔴 Regressions (core metrics, worse 2 workdays in a row)*\n" + "\n".join(_worsen_en(data))
        ))
    elif data.worsen_lines:
        # 老数据没有结构化条目：只给条数，细节看完整日报
        blocks.append(divider())
        blocks.append(section(f"*🔴 {n_worse} regression(s) in core metrics* — see full report"))

    blocks.append(divider())
    if data.tiers and data.metric_rows:
        blocks.append(two_column(_core_column(data, "ios"), _core_column(data, "android")))
    else:
        blocks.append(section("_No per-version metrics available._"))

    folds: List[Fold] = []
    if report_url:
        if data.new_crash_count:
            blocks.append(section(
                f"*🆕 {data.new_crash_count} new crash(es)* (stack traces in the full report)"
            ))
        blocks.append(context(
            f"<{report_url}|View full report →>  All metrics · New crash stacks · Top 5 crashes / jank"
        ))
    else:
        # 前端地址没配：没有地方可跳，附录只能挂 thread
        if data.new_crash_md:
            folds.append(Fold(title="🆕 New crash stacks",
                              blocks=[section("*🆕 New crash stacks*\n" + _list_body(data.new_crash_md))],
                              text="🆕 New crash stacks"))
        inner: List[dict] = []
        if data.top_crash_md:
            inner.append(section("*🔥 Top 5 crashes (by events)*\n" + _list_body(data.top_crash_md)))
        if data.top_jank_md:
            inner.append(section("*🟠 Top 5 jank (by events)*\n" + _list_body(data.top_jank_md)))
        if inner:
            folds.append(Fold(title="🔥 Top crashes / jank", blocks=inner, text="🔥 Top crashes / jank"))

    status = f"🔴 {n_worse} regression(s)" if n_worse else "no regressions"
    return Rendered(
        payload=blocks,
        folds=tuple(folds),
        # 带色条时这一行显示在消息最上面（也是锁屏推送看到的那一行），所以标题
        # 只放这里；带上恶化条数，让人不点开就能判断今天要不要看。
        text=f"*{_title_en(data)}* · {status}",
        color=_COLOR_RED if data.is_red else _COLOR_OK,
    )


def report_url_for(target_date: date) -> Optional[str]:
    """前端完整日报的深链；前端地址没配时返回 None（Slack 退回老排版）。"""
    base = ""
    try:
        from app.config import get_settings

        base = get_settings().frontend_base_url or ""
    except Exception:
        pass
    if not base:
        # 全局没配时沿用 crashguard 的探测结果（多机部署按 HOST_IP 派生），
        # 跟早报「查看完整早报」链接同一个地址
        try:
            from app.crashguard.config import get_crashguard_settings

            base = get_crashguard_settings().frontend_base_url or ""
        except Exception:
            pass
    if not base:
        return None
    return f"{base.rstrip('/')}/graygate/reports?date={target_date.isoformat()}"


async def build_report_message(target_date: date) -> Optional[Rendered]:
    """取数 + 渲染成 Slack 消息。两平台版本枚举都空时返回 `None`
    （跟 `build_report_card` 的 `available=False` 同义）。"""
    data = await collect_report_data(target_date)
    if data is None:
        return None
    return assemble_slack_message(data, report_url_for(target_date))


def assemble_focus_change_message(platform: str, old_value: str, new_value: str,
                                  operator: str) -> Rendered:
    """「主要版本」变更通知的 Slack 版（英文；对应 `focus_version.py` 里那张蓝色小卡）。"""
    plat = platform.upper()
    action = f"set to `{new_value}`" if new_value else "cleared (falls back to auto-detected top version)"
    old = f"`{old_value}`" if old_value else "not set (auto-detected)"
    title = f"🔖 [4.0.3 Gray] Primary version changed · {plat}"
    return Rendered(
        payload=[
            section(f"*{plat}* primary version {action}"),
            # operator 兜底文案是中文（"未知（调用方未提供身份）"），Slack 统一成 unknown
            context(f"Previous: {old} · Changed by: {operator if operator.isascii() else 'unknown'}"),
        ],
        text=f"*{title}*",
        color="#1D9BD1",   # 对应飞书的 template: blue
    )
