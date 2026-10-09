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
落库缓存（`report_store.py`）。没有 `report_url`（前端地址没配）时退回上面那张表的
老排版，保证信息不丢。

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
    header,
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


_PLATFORM_LABEL = {"ios": "🍎 iOS", "android": "🤖 Android"}


def _core_column_md(data: GraygateReportData, platform: str) -> str:
    """一个平台的核心指标列：大盘 / 主要版本 各列出核心指标，「不适用」的不列。"""
    core = [(k, name) for k, name, is_core in data.metric_rows if is_core]
    lines: List[str] = [f"**{_PLATFORM_LABEL[platform]}**"]
    for t in data.tiers.get(platform) or []:
        if t.label == "大盘":
            head = f"__大盘（{t.version}）__"
        elif t.version:
            head = f"__主要版本__ `{t.version}`" + ("（人工指定）" if t.manual else "")
        else:
            head = "__主要版本__"
        if t.sessions is not None:
            head += f" · {t.sessions:,} sessions"
        lines += ["", head]
        for key, name in core:
            v = t.cells.get(key, "")
            if not v or "不适用" in v:
                continue
            lines.append(f"· {name}：{v}")
    return "\n".join(lines)


def assemble_slack_message(data: GraygateReportData, report_url: Optional[str] = None) -> Rendered:
    """`GraygateReportData` → Slack 主消息 blocks（+ 无 report_url 时的 thread 折叠段）。"""
    blocks: List[dict] = [
        header(data.title),
        context(lark_md_to_mrkdwn(data.banner_md)),
    ]
    compact = bool(report_url) and bool(data.tiers) and bool(data.metric_rows)

    if data.worsen_lines:
        blocks.append(divider())
        blocks.append(section(
            "*🔴 恶化（核心指标，连续 2 个工作日同向恶化）*\n\n"
            + lark_md_to_mrkdwn("\n".join(data.worsen_lines))
        ))

    blocks.append(divider())
    if compact:
        blocks.append(two_column(
            lark_md_to_mrkdwn(_core_column_md(data, "ios")),
            lark_md_to_mrkdwn(_core_column_md(data, "android")),
        ))
    else:
        blocks.append(two_column(
            lark_md_to_mrkdwn(data.columns_md[0]),
            lark_md_to_mrkdwn(data.columns_md[1]),
        ))

    folds: List[Fold] = []
    if report_url:
        if data.new_crash_count:
            blocks.append(section(f"*🆕 新增崩溃 {data.new_crash_count} 个*（堆栈见完整日报）"))
        blocks.append(context(
            f"<{report_url}|查看完整日报 →>  全部指标 · 新增崩溃堆栈 · Top5 崩溃 / 卡顿"
        ))
    elif data.new_crash_md:
        folds.append(Fold(
            title="🆕 新增崩溃堆栈",
            blocks=[section(lark_md_to_mrkdwn(data.new_crash_md))],
            text="🆕 新增崩溃堆栈",
        ))
    if not report_url and (data.top_crash_md or data.top_jank_md):
        inner: List[dict] = []
        if data.top_crash_md:
            inner.append(section(lark_md_to_mrkdwn(data.top_crash_md)))
        if data.top_jank_md:
            inner.append(section(lark_md_to_mrkdwn(data.top_jank_md)))
        folds.append(Fold(title="🔥 Top 崩溃 / 卡顿", blocks=inner,
                          text="🔥 Top 崩溃 / 卡顿"))

    return Rendered(
        payload=blocks,
        folds=tuple(folds),
        # text 是通知栏/推送看到的那一行。带上恶化条数，让人在锁屏上就能判断
        # 要不要现在点开——只写"灰度日报"的话每天长一个样，很快就被无视了。
        text=(f"{data.title} · 🔴 {len(data.worsen_lines)} 项恶化"
              if data.worsen_lines else f"{data.title} · 无恶化"),
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


def assemble_focus_change_message(platform: str, action: str,
                                  old_note: str, operator: str) -> Rendered:
    """「主要版本」变更通知的 Slack 版（对应 `focus_version.py` 里那张蓝色小卡）。"""
    return Rendered(
        payload=[
            header(f"🔖 4.0.3 灰度「主要版本」变更 · {platform.upper()}"),
            section(f"*{platform.upper()}* {lark_md_to_mrkdwn(action)}"),
            context(f"{lark_md_to_mrkdwn(old_note)} · 操作人：{operator}"),
        ],
        text=f"🔖 4.0.3 灰度「主要版本」变更 · {platform.upper()}",
        color="#1D9BD1",   # 对应飞书的 template: blue
    )
