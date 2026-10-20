"""Graygate 日报的网页端完整版（GFM markdown）。

和飞书卡片 / Slack 消息消费同一份 `GraygateReportData`，不重新取数。Slack 主消息只放
核心指标，「查看完整日报 →」跳到前端 `/graygate/reports?date=...`，那里渲染的就是
这里拼出来的 markdown（发送时落库缓存，见 `report_store.py`）。

和飞书卡片的区别：飞书是 iOS / Android 双列 bullet，网页端宽度够，改成
「指标 × 平台口径」一张表，四个口径横向对比；核心指标（参与恶化判定的）排在前面，
其余指标放在分隔行之后。2026-10-20 起全英文。
"""
from __future__ import annotations

from typing import List, Optional

from app.graygate.services.card_builder import GraygateReportData, TierSummary
from app.graygate.services.slack_report import (
    _PLATFORM_EN as _PLATFORM_LABEL,
    _TIER_EN,
    _banner_en,
    _cell_en,
    _metric_en,
    _worsen_en,
)

# 2026-10-20：网页端完整日报改全英文（用户要求），文案与 Slack 版共用一套英文映射。
_SECTION_EN = {
    "new_crash": "🆕 New crash stacks",
    "top_crash": "🔥 Top 5 crashes (by events, new or not)",
    "top_jank": "🟠 Top 5 jank (by events, new or not)",
}


def _section(md: Optional[str], title: str) -> List[str]:
    """`**中文标题**\n\n- ...` 这种片段：标题换成英文 `##`，正文（平台/版本/events/
    英文 issue 标题）原样。"""
    if not md:
        return []
    _, _, body = md.partition("\n")
    return ["", f"## {title}", "", body.strip(), ""]


def _cell(text: str) -> str:
    # 表格单元格里的 | 会把列切断
    return _cell_en(text).replace("|", "\\|")


def _version_cell(t: TierSummary) -> str:
    if not t.version:
        return "—"
    return f"`{t.version}`" + (" (manual)" if t.manual else "")


def _metrics_table(data: GraygateReportData) -> List[str]:
    # 2026-10-20 用户要求：两个平台的主要版本相邻（方便直接对比灰度包），大盘排在后面：
    # iOS·Primary | Android·Primary | iOS·Overall | Android·Overall
    cols: List[tuple] = [
        (p, t)
        for want in ("主要版本", "大盘")
        for p in ("ios", "android")
        for t in (data.tiers.get(p) or [])
        if t.label == want
    ]
    header = "| Metric | " + " | ".join(
        f"{_PLATFORM_LABEL[p]} · {_TIER_EN.get(t.label, t.label)}" for p, t in cols
    ) + " |"
    lines = [header, "|---" * (len(cols) + 1) + "|"]
    lines.append("| Version | " + " | ".join(_version_cell(t) for _, t in cols) + " |")
    lines.append("| Sessions | " + " | ".join(
        (t.display_sessions or "—") for _, t in cols
    ) + " |")
    lines.append("| Users | " + " | ".join(
        f"{t.users:,}" if t.users is not None else "—" for _, t in cols
    ) + " |")

    core = [r for r in data.metric_rows if r[2]]
    other = [r for r in data.metric_rows if not r[2]]
    for key, name, _ in core:
        lines.append(f"| **{_metric_en(key, name)}** | " + " | ".join(
            _cell(t.cells.get(key, "—")) for _, t in cols) + " |")
    if other:
        lines.append("| _—— Other metrics (not used for regression detection) ——_ |" + " |" * len(cols))
        for key, name, _ in other:
            lines.append(f"| {_metric_en(key, name)} | " + " | ".join(
                _cell(t.cells.get(key, "—")) for _, t in cols) + " |")
    return lines


def _version_rows_md(data: GraygateReportData) -> List[str]:
    """`version_pattern` 下每个 build 的会话 / 用户 / 采样率——Slack 只给大盘 + 主要版本，
    完整数据在这里。"""
    if not any(data.version_rows.values()):
        return []
    lines = ["", f"## 📦 All `{data.version_pattern}` versions — sessions / users", ""]
    sampled = False
    for p in ("ios", "android"):
        rows = data.version_rows.get(p) or []
        if not rows:
            continue
        total = sum(r.est_sessions for r in rows) or 1
        lines += [f"### {_PLATFORM_LABEL[p]}", "",
                  "| Version | Sessions | Share | Users | Sample rate |", "|---|---|---|---|---|"]
        for r in rows:
            approx = "≈" if r.sample_rate < 100 else ""
            sampled = sampled or r.sample_rate < 100
            lines.append(
                f"| `{r.version}` | {approx}{r.est_sessions:,} | {r.est_sessions / total * 100:.1f}% | "
                f"{f'{r.users:,}' if r.users is not None else '—'} | {r.sample_rate:g}% |"
            )
        lines.append("")
    if sampled:
        lines.append("> ⚠️ Datadog RUM sampling: sessions are extrapolated by sample rate (≈); "
                     "users are distinct within the sample and undercounted.")
        lines.append("")
    return lines


def assemble_report_markdown(data: GraygateReportData) -> str:
    lines: List[str] = [f"> {_banner_en(data)}", ""]

    if data.worsen_items:
        lines += ["## 🔴 Regressions (core metrics, worse 2 workdays in a row)", ""]
        lines += [f"- {w.lstrip('• ')}" for w in _worsen_en(data)]
        lines.append("")
    elif data.worsen_lines:
        # 老数据没有结构化条目，只能给条数
        lines += [f"## 🔴 {len(data.worsen_lines)} regression(s) in core metrics", ""]

    lines += ["## 📊 Metrics overview", ""]
    if data.tiers and data.metric_rows:
        lines += _metrics_table(data)
        lines += ["", "🟩 / 🟥 = meets / misses target (per metrics.yaml); **bold** = core metric"]
    else:
        # 老数据没有结构化分层：退回飞书那两列原文
        # （"· 指标：值" 是逐行的，markdown 里要硬换行才不会糊成一段）
        for col in data.columns_md:
            lines += ["", col.replace("\n", "  \n"), ""]

    lines += _version_rows_md(data)
    lines += _section(data.new_crash_md, _SECTION_EN["new_crash"])
    lines += _section(data.top_crash_md, _SECTION_EN["top_crash"])
    lines += _section(data.top_jank_md, _SECTION_EN["top_jank"])
    return "\n".join(lines).strip() + "\n"
