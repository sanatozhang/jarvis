"""Demo 阶段飞书卡片（无 ack 按钮，正式版见 design §6）。

卡片标题前缀 `[coreguard·demo]` 与 crashguard 区分。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def build_demo_alert_card(
    metric_title: str,
    current_value: float,
    baseline_value: Optional[float],
    change_pp: Optional[float],
    threshold_pp: float,
    sessions_count: Optional[int],
    current_window_label: str,
    baseline_window_label: str,
    dashboard_url: str,
    forced: bool = False,
) -> Dict[str, Any]:
    """飞书 v2 interactive card。

    forced=True 时 (force_alert query param 触发) 加 "🧪 演示" 标记，避免被当成真告警。
    """
    # 卡片头部颜色：恶化 → red，演示 → blue
    template = "blue" if forced else ("red" if (change_pp is not None and change_pp <= -threshold_pp) else "yellow")
    title = f"[coreguard·demo] {metric_title}"
    if forced:
        title += " (🧪 强制演示)"

    def _fmt(v: Optional[float], suffix: str = "") -> str:
        return "N/A" if v is None else f"{v:.3f}{suffix}"

    fields = [
        {
            "is_short": True,
            "text": {
                "tag": "lark_md",
                "content": f"**当前窗口**\n{current_window_label}\n值: {_fmt(current_value, '%')}",
            },
        },
        {
            "is_short": True,
            "text": {
                "tag": "lark_md",
                "content": f"**上周同时段（SHoW）**\n{baseline_window_label}\n值: {_fmt(baseline_value, '%')}",
            },
        },
        {"is_short": False, "text": {"tag": "lark_md", "content": "---"}},
        {
            "is_short": True,
            "text": {
                "tag": "lark_md",
                "content": f"**变化**\n{_fmt(change_pp, ' pp')} (阈值 {threshold_pp} pp)",
            },
        },
        {
            "is_short": True,
            "text": {
                "tag": "lark_md",
                "content": f"**Sessions（当前窗口）**\n{sessions_count if sessions_count is not None else 'N/A'}",
            },
        },
    ]

    card = {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {"tag": "div", "fields": fields},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "lark_md",
                        "content": (
                            f"📊 [打开 Datadog Dashboard]({dashboard_url})  "
                            f"·  Demo 模式 — 正式版含 Ack / 升级 / 自动恢复"
                        ),
                    }
                ],
            },
        ],
    }
    return card
