"""
API routes for daily report generation and history.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.db import database as db

logger = logging.getLogger("jarvis.api.reports")
router = APIRouter()


@router.get("/daily/{date_str}")
async def get_daily_report(date_str: str):
    """
    Generate a daily report for the given date (YYYY-MM-DD).
    Returns structured data + Markdown.
    """
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    analyses = await db.get_analyses_by_date(date_str)

    if not analyses:
        return {
            "date": date_str,
            "total_issues": 0,
            "analyses": [],
            "category_stats": {},
            "markdown": f"# 值班汇总报告\n\n**日期**：{date_str}\n**工单数**：0\n\n暂无已分析工单。",
        }

    # Build category stats
    category_stats: dict[str, int] = {}
    analysis_list = []
    for a in analyses:
        pt = a.problem_type or "未分类"
        category_stats[pt] = category_stats.get(pt, 0) + 1

        evidence = []
        try:
            evidence = json.loads(a.key_evidence_json) if a.key_evidence_json else []
        except Exception:
            pass

        analysis_list.append({
            "task_id": a.task_id,
            "issue_id": a.issue_id,
            "problem_type": a.problem_type,
            "root_cause": a.root_cause,
            "confidence": a.confidence,
            "key_evidence": evidence,
            "user_reply": a.user_reply,
            "needs_engineer": a.needs_engineer,
            "rule_type": a.rule_type,
            "agent_type": a.agent_type,
            "created_at": a.created_at.isoformat() if a.created_at else "",
        })

    # Generate Markdown
    md = _generate_markdown(date_str, analysis_list, category_stats)

    return {
        "date": date_str,
        "total_issues": len(analyses),
        "analyses": analysis_list,
        "category_stats": category_stats,
        "markdown": md,
    }


@router.get("/daily/{date_str}/markdown", response_class=PlainTextResponse)
async def get_daily_report_markdown(date_str: str):
    """Get the daily report as downloadable Markdown."""
    report = await get_daily_report(date_str)
    return report["markdown"]


@router.get("/dates")
async def list_report_dates(limit: int = Query(30, le=90)):
    """List dates that have analysis results."""
    # Simple approach: get recent tasks and extract unique dates
    tasks = await db.list_tasks(limit=200)
    dates = set()
    for t in tasks:
        if t.status == "done" and t.created_at:
            dates.add(t.created_at.strftime("%Y-%m-%d"))
    return {"dates": sorted(dates, reverse=True)[:limit]}


def _generate_markdown(
    date_str: str,
    analyses: list,
    category_stats: dict,
) -> str:
    """Generate the daily summary Markdown report."""
    lines = [
        f"# 值班汇总报告\n",
        f"**值班日期**：{date_str}",
        f"**处理工单数**：{len(analyses)} 个\n",
        "---\n",
        "## 一、工单汇总表\n",
        "| # | Issue ID | 问题类型 | 置信度 | 一句话归因 | 需工程师 |",
        "|---|----------|---------|--------|-----------|---------|",
    ]

    for i, a in enumerate(analyses, 1):
        engineer = "是" if a.get("needs_engineer") else "否"
        cause = (a.get("root_cause") or "")[:60]
        lines.append(
            f"| {i} | {a['issue_id'][:12]} | {a['problem_type']} | {a['confidence']} | {cause} | {engineer} |"
        )

    lines.append("\n---\n")
    lines.append("## 二、问题分类统计\n")
    lines.append("| 问题类型 | 数量 |")
    lines.append("|---------|------|")
    for ptype, count in sorted(category_stats.items(), key=lambda x: -x[1]):
        lines.append(f"| {ptype} | {count} |")

    lines.append("\n---\n")
    lines.append("## 三、详细分析\n")

    for i, a in enumerate(analyses, 1):
        lines.append(f"### 工单 {i}：{a['issue_id'][:12]}\n")
        lines.append(f"**问题类型**：{a['problem_type']}")
        lines.append(f"**置信度**：{a['confidence']}")
        lines.append(f"**规则**：{a['rule_type']}　**Agent**：{a['agent_type']}\n")
        lines.append("**分析结果**\n")
        lines.append(a.get("root_cause", "无") + "\n")

        evidence = a.get("key_evidence", [])
        if evidence:
            lines.append("**关键证据**\n")
            for ev in evidence[:5]:
                lines.append(f"- `{ev[:120]}`")
            lines.append("")

        reply = a.get("user_reply", "")
        if reply:
            lines.append("**用户回复（可直接复制）**\n")
            lines.append(f"> {reply.replace(chr(10), chr(10) + '> ')}\n")

        lines.append("---\n")

    lines.append("## 四、待办事项\n")
    engineer_issues = [a for a in analyses if a.get("needs_engineer")]
    if engineer_issues:
        for a in engineer_issues:
            lines.append(f"- [ ] 工程师确认：{a['issue_id'][:12]} ({a['problem_type']})")
    else:
        lines.append("- 无需工程师处理的工单")

    return "\n".join(lines)
