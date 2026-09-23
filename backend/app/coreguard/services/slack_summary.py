"""Coreguard 告警卡片的 Slack Block Kit 渲染。

跟 graygate 不同的地方：coreguard 的卡片**不需要取数拆分** ——
`feishu_summary_card.build_summary_card()` 收的本来就是已经算好的
`breached` / `healthy` / `errored` 三个 dict 列表，两个渲染器直接消费同一批
入参即可，没有"会被跑两遍的昂贵查询"。

所以这里复用飞书侧那几个**纯 markdown 产物**的函数（`_headline`、
`_breached_block`、`_fmt_*`），只换外层结构 + 语法转换。复制一份改语法等于
把"σ 怎么算、方向词怎么选、阈值怎么格式化"这些判断也复制一份，两边必然漂移。

## 主消息 / thread 的划分

coreguard 是**告警**不是日报，异常列表就是全部内容，所以默认全部放主消息。
只有一种情况会用到 thread：异常条数多到撑爆 Block Kit 的 50 block 上限
（`_MAX_INLINE_BREACHES`）。这是个防御性的兜底，正常情况下（18 项指标里
breach 几项）走不到。
"""
from __future__ import annotations

import logging
from datetime import timezone as _tz
from typing import Any, Dict, List

from app.coreguard.services.feishu_summary_card import (
    _breached_block,
    _build_dashboard_url,
    _headline,
)
from app.services.im.base import Fold, Rendered
from app.services.im.mrkdwn import (
    context,
    divider,
    header,
    lark_md_to_mrkdwn,
    section,
)

logger = logging.getLogger("coreguard.slack_summary")

# 飞书 header template → Slack attachment 色条。
_COLOR_BY_TEMPLATE = {
    "red": "#E01E5A",
    "blue": "#1D9BD1",
    "green": "#2EB67D",
}

# 主消息里最多平铺多少条异常，超出的进 thread。
#
# 上限本身是 50 blocks；这里留足余量给 header/headline/窗口/缺数据/按钮
# （固定 6 个左右）。撑爆的后果是 `invalid_blocks` —— **整条告警发不出去**，
# 而这恰恰发生在"异常特别多"也就是最该收到告警的时候。
_MAX_INLINE_BREACHES = 30


def _utc_ms(dt) -> int:
    """cur_start 等是 `datetime.utcnow()` 的 naive UTC，naive `.timestamp()`
    会按本地时区解释、偏 8h，必须显式打 UTC tzinfo（跟飞书侧同一处理）。"""
    return int(dt.replace(tzinfo=_tz.utc).timestamp() * 1000)


def build_summary_message(
    cur_start, cur_end, base_start, base_end,
    breached: List[Dict[str, Any]],
    healthy: List[Dict[str, Any]],
    errored: List[Dict[str, Any]],
    forced: bool,
    dashboard_id: str,
    datadog_site: str,
) -> Rendered:
    """签名跟 `build_summary_card` **逐字对齐**，方便调用方按 provider 二选一。"""
    n_breach, n_healthy, n_err = len(breached), len(healthy), len(errored)
    total = n_breach + n_healthy + n_err

    if n_breach > 0:
        template = "red"
        title = f"[coreguard] ⚠️ 核心指标异常告警 ({n_breach}/{total})"
    elif forced:
        template = "blue"
        title = f"[coreguard] 🧪 演示 — {total} 项全部正常"
    else:
        template = "green"
        title = f"[coreguard] ✅ {total} 项核心指标全部正常"

    cur_start_ms, cur_end_ms = _utc_ms(cur_start), _utc_ms(cur_end)
    base_start_ms, base_end_ms = _utc_ms(base_start), _utc_ms(base_end)
    dashboard_url = (
        f"https://app.{datadog_site}/dashboard/{dashboard_id}"
        f"?from_ts={cur_start_ms}&to_ts={cur_end_ms}&live=false"
    )
    baseline_days = max((cur_start - base_start).days, 1)

    blocks: List[dict] = [
        header(title),
        section(f"📢 {lark_md_to_mrkdwn(_headline(breached))}"),
        context(
            f"当前窗口 {cur_start.strftime('%m-%d %H:%M')} ~ {cur_end.strftime('%H:%M')} UTC"
            f"  ·  基线 近 {baseline_days} 天同时段（预测带 median±k·MAD）"
            f"  ·  共评估 {total} 项 (异常 {n_breach}"
            f"{('，缺数据 ' + str(n_err)) if n_err else ''})"
        ),
    ]

    folds: List[Fold] = []
    if breached:
        blocks.append(divider())
        # P0 在前，同 tier 按偏离幅度降序 —— 跟飞书侧同一排序，不要各排各的
        ordered = sorted(
            breached,
            key=lambda x: (0 if x["tier"] == "P0" else 1, -(abs(x.get("change") or 0))),
        )

        def _block_for(r: Dict[str, Any]) -> dict:
            return section(lark_md_to_mrkdwn(_breached_block(
                r, cur_start_ms, cur_end_ms, base_start_ms, base_end_ms,
                dashboard_id, datadog_site,
            )))

        blocks.extend(_block_for(r) for r in ordered[:_MAX_INLINE_BREACHES])
        overflow = ordered[_MAX_INLINE_BREACHES:]
        if overflow:
            logger.warning(
                "coreguard 异常 %d 条，超出主消息平铺上限 %d —— 其余 %d 条进 thread",
                len(ordered), _MAX_INLINE_BREACHES, len(overflow),
            )
            folds.append(Fold(
                title=f"还有 {len(overflow)} 项异常",
                blocks=[_block_for(r) for r in overflow],
                text=f"⚠️ 还有 {len(overflow)} 项异常",
            ))

    if errored and n_err > 0:
        names = "、".join(r["title"] for r in errored[:5])
        if n_err > 5:
            names += f" 等 {n_err} 项"
        blocks.append(context(f"⚪ 缺数据：{names}"))

    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "📊 打开 Datadog Dashboard 排查",
                     "emoji": True},
            "style": "primary",
            "url": dashboard_url,
        }],
    })

    return Rendered(
        payload=blocks,
        folds=tuple(folds),
        text=title,
        color=_COLOR_BY_TEMPLATE.get(template, ""),
    )
