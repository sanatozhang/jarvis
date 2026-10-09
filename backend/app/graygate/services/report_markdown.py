"""Graygate 日报的网页端完整版（GFM markdown）。

和飞书卡片 / Slack 消息消费同一份 `GraygateReportData`，不重新取数。Slack 主消息只放
核心指标，「查看完整日报 →」跳到前端 `/graygate/reports?date=...`，那里渲染的就是
这里拼出来的 markdown（发送时落库缓存，见 `report_store.py`）。

和飞书卡片的区别：飞书是 iOS / Android 双列 bullet，网页端宽度够，改成
「指标 × 平台口径」一张表，四个口径横向对比；核心指标（参与恶化判定的）排在前面，
其余指标放在分隔行之后。
"""
from __future__ import annotations

from typing import List, Optional

from app.graygate.services.card_builder import GraygateReportData, TierSummary

_PLATFORM_LABEL = {"ios": "🍎 iOS", "android": "🤖 Android"}


def _section(md: Optional[str]) -> List[str]:
    """`**🆕 新增崩溃堆栈**\\n\\n- ...` 这种片段：首行加粗标题改成 `##`，正文原样。"""
    if not md:
        return []
    head, _, body = md.partition("\n")
    title = head.strip().strip("*").strip()
    return ["", f"## {title}", "", body.strip(), ""]


def _cell(text: str) -> str:
    # 表格单元格里的 | 会把列切断
    return text.replace("|", "\\|")


def _version_cell(t: TierSummary) -> str:
    if not t.version:
        return "—"
    return f"`{t.version}`" + ("（人工指定）" if t.manual else "")


def _metrics_table(data: GraygateReportData) -> List[str]:
    # 每个平台主要版本在前、大盘在后（主要版本是这次灰度真正在看的包）
    cols: List[tuple] = [
        (p, t) for p in ("ios", "android")
        for t in sorted(data.tiers.get(p) or [], key=lambda t: 0 if t.label == "主要版本" else 1)
    ]
    header = "| 指标 | " + " | ".join(f"{_PLATFORM_LABEL[p]} · {t.label}" for p, t in cols) + " |"
    lines = [header, "|---" * (len(cols) + 1) + "|"]
    lines.append("| 版本 | " + " | ".join(_version_cell(t) for _, t in cols) + " |")
    lines.append("| Sessions | " + " | ".join(
        f"{t.sessions:,}" if t.sessions is not None else "—" for _, t in cols
    ) + " |")

    core = [r for r in data.metric_rows if r[2]]
    other = [r for r in data.metric_rows if not r[2]]
    for key, name, _ in core:
        lines.append(f"| **{_cell(name)}** | " + " | ".join(
            _cell(t.cells.get(key, "—")) for _, t in cols) + " |")
    if other:
        lines.append("| _—— 其他指标（不参与恶化判定）——_ |" + " |" * len(cols))
        for key, name, _ in other:
            lines.append(f"| {_cell(name)} | " + " | ".join(
                _cell(t.cells.get(key, "—")) for _, t in cols) + " |")
    return lines


def assemble_report_markdown(data: GraygateReportData) -> str:
    lines: List[str] = [f"> {data.banner_md}", ""]

    if data.worsen_lines:
        lines += ["## 🔴 恶化（核心指标，连续 2 个工作日同向恶化）", ""]
        lines += data.worsen_lines
        lines.append("")

    lines += ["## 📊 指标总览", ""]
    if data.tiers and data.metric_rows:
        lines += _metrics_table(data)
        lines += ["", "🟩 / 🟥 = 达标 / 未达标（对照 metrics.yaml 的 target）；加粗为核心指标"]
    else:
        # 老数据没有结构化分层：退回飞书那两列原文
        # （"· 指标：值" 是逐行的，markdown 里要硬换行才不会糊成一段）
        for col in data.columns_md:
            lines += ["", col.replace("\n", "  \n"), ""]

    lines += _section(data.new_crash_md)
    lines += _section(data.top_crash_md)
    lines += _section(data.top_jank_md)
    return "\n".join(lines).strip() + "\n"
