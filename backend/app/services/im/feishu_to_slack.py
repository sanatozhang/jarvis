"""把**已经构造好的**飞书 interactive card 编译成 Slack Block Kit。

## 这跟设计文档否决的「中立卡片 IR」不是一回事

否决的那个方案是：**重写 12 个 builder**，让它们吐一层中立结构，两个 provider
各自编译。它的代价是动 ~2300 行**当下唯一在跑**的渲染代码，可能今天就把飞书
早晚报改坏。

这个编译器的输入是飞书 builder 的**输出**，所以：

- 现有渲染代码**一行不动**，飞书路径的风险为零
- 映射只有一份，9 张卡自动全覆盖
- 飞书侧下线时，连同这个文件一起删

代价是 Slack 侧的结构是从飞书结构推导出来的，用不上 Block Kit 独有的花样。
对 crashguard 来说这个取舍是划算的：9 张卡手写 9 个渲染器，成本远高于收益，
而且烤期内每改一次文案就要改两遍。

graygate（1 张卡）和 coreguard（2 张卡）**没有**走这条路——它们的版面值得
专门设计（双列指标表 / 告警布局），手写更好。判据是卡的数量和版面独特性，
不是"统一"。

## 映射表

| 飞书 | Slack |
|---|---|
| `header.template` | attachment `color` 色条 |
| `header.title.content` | `header` block（plain_text，≤150） |
| `div` + `lark_md` / `markdown` | `section`（mrkdwn） |
| `hr` | `divider` |
| `note` | `context` |
| `action` + `button` | `actions` + URL button |
| `column_set`（2 列） | `section.fields`（两列） |
| `column_set`（>2 列） | 逐列拍平成多个 `section` |
| **`collapsible_panel`** | **一条 thread 回复**（Block Kit 没有折叠区） |

`collapsible_panel` → thread 是整个迁移里最关键的一条映射：飞书用折叠表达
"这段是 FYI，需要时再展开"，Slack 的 thread 天然是同一个语义。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from app.services.im.base import Fold, Rendered
from app.services.im.mrkdwn import (
    MAX_BLOCKS,
    clip,
    context,
    divider,
    header,
    lark_md_to_mrkdwn,
    section,
    two_column,
)

logger = logging.getLogger("jarvis.services.im.feishu_to_slack")

# 飞书 header template → Slack attachment 色条。
# 未知 template 不给色条（而不是瞎猜一个）——颜色传达的是严重度，猜错比没有更糟。
_COLOR_BY_TEMPLATE = {
    "red": "#E01E5A",
    "orange": "#ECB22E",
    "yellow": "#ECB22E",
    "carmine": "#E01E5A",
    "blue": "#1D9BD1",
    "indigo": "#1D9BD1",
    "green": "#2EB67D",
    "turquoise": "#2EB67D",
    "grey": "#868686",
    "default": "",
}


def _text_of(el: Dict[str, Any]) -> str:
    """从一个飞书元素里取出文本内容，兼容几种写法。"""
    t = el.get("text")
    if isinstance(t, dict):
        return t.get("content", "") or ""
    if isinstance(t, str):
        return t
    # markdown 组件把内容直接挂在 content 上
    return el.get("content", "") or ""


def _button_url(btn: Dict[str, Any]) -> str:
    """取按钮的跳转 URL，兼容飞书的三种写法。

    | 写法 | 出现在 |
    |---|---|
    | `url` | v1 的 action/button（几个告警卡） |
    | `behaviors: [{type: open_url, default_url}]` | v2 的 button（早晚报底部） |
    | `multi_url.url` | 飞书的多端差异化链接 |

    只认 `url` 的话，v2 那种按钮会被当成回调按钮丢掉——而它其实是个正常的
    跳转链接。
    """
    if btn.get("url"):
        return btn["url"]
    for b in btn.get("behaviors") or []:
        if b.get("type") == "open_url":
            # default_url 是兜底，其余是分端覆盖；桌面端优先级最接近 Slack 的场景
            for key in ("default_url", "pc_url", "web_url", "url"):
                if b.get(key):
                    return b[key]
    multi = btn.get("multi_url") or {}
    for key in ("url", "pc_url", "web_url"):
        if multi.get(key):
            return multi[key]
    return ""


def _compile_elements(elements: List[Dict[str, Any]]) -> Tuple[List[dict], List[Fold]]:
    """递归编译一组飞书元素 → (blocks, folds)。

    folds 是**从任意深度收集上来的** `collapsible_panel`——飞书允许把折叠区
    嵌在 column 里，而 Slack 的 thread 是消息级的，没有"嵌套的 thread"。
    收集到顶层是唯一的选择，顺序按出现顺序。
    """
    blocks: List[dict] = []
    folds: List[Fold] = []

    for el in elements or []:
        tag = el.get("tag")

        if tag == "hr":
            blocks.append(divider())

        elif tag in ("div", "markdown"):
            content = _text_of(el)
            if content:
                blocks.append(section(lark_md_to_mrkdwn(content)))

        elif tag == "note":
            parts = [_text_of(x) for x in (el.get("elements") or [])]
            joined = "  ".join(p for p in parts if p)
            if joined:
                blocks.append(context(lark_md_to_mrkdwn(joined)))

        elif tag in ("action", "button"):
            # 飞书两种写法都有：v1 是 `action` 容器包一组 `button`；
            # v2 允许 `button` **直接作为 element**（crashguard 早晚报就是这样，
            # 见 feishu_card.py「底部按钮（v2 schema）」那段）。只认 action 的话
            # 早晚报的按钮会被静默丢掉——2026-09-23 真机验证时就是这么发现的。
            raw_buttons = (el.get("actions") or []) if tag == "action" else [el]
            buttons = []
            for a in raw_buttons:
                label = _text_of(a) or "打开"
                url = _button_url(a)
                if not url:
                    # 没有 url 的按钮是回调按钮，需要 interactivity + HMAC 端点，
                    # 本次迁移刻意不做。丢掉按钮但保留日志——静默丢会让人以为
                    # 是渲染 bug。
                    logger.warning("feishu_to_slack: 丢弃一个非 URL 按钮「%s」"
                                   "（Slack 侧需要 interactivity，本期不支持）", label)
                    continue
                buttons.append({
                    "type": "button",
                    # button text 上限 75 字
                    "text": {"type": "plain_text", "text": clip(label, 75, marker="…"),
                             "emoji": True},
                    "url": url,
                })
            if buttons:
                # actions block 最多 25 个元素
                blocks.append({"type": "actions", "elements": buttons[:25]})

        elif tag == "column_set":
            cols = el.get("columns") or []
            rendered_cols: List[str] = []
            for col in cols:
                sub_blocks, sub_folds = _compile_elements(col.get("elements") or [])
                folds.extend(sub_folds)
                # 列内部只可能是文本段，拼成一段 mrkdwn 塞进 field
                rendered_cols.append("\n".join(
                    b.get("text", {}).get("text", "")
                    for b in sub_blocks if b.get("type") == "section"
                ).strip())
            rendered_cols = [c for c in rendered_cols if c]
            if len(rendered_cols) == 2:
                blocks.append(two_column(rendered_cols[0], rendered_cols[1]))
            else:
                # Block Kit 的 fields 固定两列。>2 列拍平成多个 section
                # ——挤成两列会让每格都被截断，不如老老实实竖排。
                blocks.extend(section(c) for c in rendered_cols)

        elif tag == "collapsible_panel":
            title_md = ""
            hdr = el.get("header") or {}
            if isinstance(hdr.get("title"), dict):
                title_md = hdr["title"].get("content", "") or ""
            title = lark_md_to_mrkdwn(title_md) or "详情"
            inner_blocks, inner_folds = _compile_elements(el.get("elements") or [])
            # 嵌套折叠拍平到同一层：Slack 没有"thread 里的 thread"
            folds.extend(inner_folds)
            if inner_blocks:
                folds.append(Fold(
                    title=title,
                    # 折叠段自己带个小标题，否则 thread 里只看到一堆数字
                    blocks=[section(f"*{title}*"), *inner_blocks],
                    text=title,
                ))

        elif tag in ("img", "image"):
            # 图片要先上传拿 file id，本期范围外（jarvis 这三个模块没有图片卡片）。
            logger.warning("feishu_to_slack: 跳过 img 元素（本期不支持图片）")

        else:
            logger.warning("feishu_to_slack: 未知元素 tag=%r，已跳过", tag)

    return blocks, folds


def compile_card(card: Dict[str, Any]) -> Rendered:
    """飞书 interactive card → `Rendered`（Slack blocks + thread folds + 色条）。"""
    hdr = card.get("header") or {}
    title = ""
    if isinstance(hdr.get("title"), dict):
        title = hdr["title"].get("content", "") or ""
    template = (hdr.get("template") or "").strip().lower()

    # v2 schema 把元素放在 body.elements，v1 直接是 elements
    elements = ((card.get("body") or {}).get("elements")
                if isinstance(card.get("body"), dict) else None)
    if elements is None:
        elements = card.get("elements") or []

    blocks, folds = _compile_elements(elements)
    if title:
        blocks.insert(0, header(title))

    # 撑爆 50 blocks 的后果是 invalid_blocks —— **整条消息发不出去**。
    # 溢出的部分进 thread 而不是直接丢：丢掉的恰恰是排在最后的详情段。
    if len(blocks) > MAX_BLOCKS:
        overflow = blocks[MAX_BLOCKS - 1:]
        logger.warning("feishu_to_slack: 编译出 %d 个 block，超过上限 %d —— "
                       "尾部 %d 个移进 thread", len(blocks), MAX_BLOCKS, len(overflow))
        blocks = blocks[:MAX_BLOCKS - 1]
        folds.insert(0, Fold(title="（续）", blocks=overflow[:MAX_BLOCKS], text="（续）"))

    return Rendered(
        payload=blocks,
        folds=tuple(folds),
        text=title or "jarvis 通知",
        color=_COLOR_BY_TEMPLATE.get(template, ""),
    )
