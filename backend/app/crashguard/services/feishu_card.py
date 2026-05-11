"""
飞书 Interactive Card 构造器（早晚报专用）。

输入 daily_report.compose_report() 生成的 markdown 已不够用——飞书富文本卡片需要结构化数据。
为此，我们直接基于 compose_report 的 payload + 复用一些原始数据，构造卡片 schema。

为了简化：把整篇 markdown 拆成段（## 大标题 → header），其余作为 lark_md content；
最后加一个"在 Web 查看完整报告"按钮。
"""
from __future__ import annotations

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
