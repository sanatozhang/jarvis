"""飞书 `lark_md` → Slack `mrkdwn` 的文本转换。

**为什么是转换而不是各写一份**：各模块的 markdown 片段（指标表格行、Top 崩溃
列表、恶化摘要…）是**数据装配**的产物，跟渲染无关的部分占九成——把
`_tier_md()` / `_build_top_crash_md()` 这类函数复制一份改语法，等于把数据
装配逻辑也复制一份，两边会立刻漂移。设计文档否决的是中立**卡片 IR**
（结构层），不是这个纯文本语法转换。

三条语法差异（其余部分两边一致）：

| | 飞书 lark_md | Slack mrkdwn |
|---|---|---|
| 加粗 | `**x**` | `*x*` |
| 下划线/强调 | `__x__` | `_x_` |
| 链接 | `[text](url)` | `<url|text>` |

⚠️ **顺序不能换**：链接必须先转。链接的 text 里可能含 `*` 或 `_`（比如
`[**崩溃** →](url)`），先转加粗会把 `](` 之间的内容也动了，再转链接时
正则就匹配不上了。
"""
from __future__ import annotations

import re

_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_UNDERLINE = re.compile(r"__(.+?)__", re.S)


def lark_md_to_mrkdwn(text: str) -> str:
    """把一段飞书 lark_md 转成 Slack mrkdwn。空串原样返回。"""
    if not text:
        return ""
    out = _LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    out = _BOLD.sub(r"*\1*", out)
    out = _UNDERLINE.sub(r"_\1_", out)
    return out


# ---------------------------------------------------------------------------
# Block Kit 上限
#
# 这些上限是**静默截断或 invalid_blocks**，不是温和的降级——超了要么内容
# 没了、要么整条消息发不出去。所以构造方必须主动裁，不能指望 Slack 帮忙。
# ---------------------------------------------------------------------------
MAX_BLOCKS = 50
MAX_SECTION_TEXT = 3000
MAX_FIELD_TEXT = 2000
MAX_FIELDS_PER_SECTION = 10
MAX_HEADER_TEXT = 150


def clip(text: str, limit: int, *, marker: str = "\n…（已截断）") -> str:
    """按 limit 裁剪，裁掉时在末尾留痕。

    留痕不是装饰：不留的话"内容被截断"和"内容本来就这么短"在界面上完全
    分不出来，而前者是要去改代码的 bug。
    """
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": clip(text, MAX_SECTION_TEXT)}}


def context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": clip(text, MAX_SECTION_TEXT)}]}


def header(text: str) -> dict:
    # header 只吃 plain_text，且不解析 mrkdwn —— 把语法标记原样显示出来会很难看，
    # 所以这里不做转换，调用方应当直接给纯文本。
    return {"type": "header",
            "text": {"type": "plain_text", "text": clip(text, MAX_HEADER_TEXT, marker="…"),
                     "emoji": True}}


def divider() -> dict:
    return {"type": "divider"}


def two_column(left: str, right: str) -> dict:
    """飞书 `column_set` 双列的对等物。

    Block Kit 只有 `section.fields` 这一种多列，固定两列、每个 field ≤2000 字。
    超长的那一侧会被 `clip()` 裁掉尾巴——对 graygate 的指标表来说（两层口径
    × 11 个指标 ≈ 900 字）有很大余量，但 crashguard 的列更长，接那边时要先量。
    """
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": clip(left, MAX_FIELD_TEXT)},
            {"type": "mrkdwn", "text": clip(right, MAX_FIELD_TEXT)},
        ],
    }
