"""
Crashguard 批量分析 (Sprint 2.1)。

逻辑：
- 取今日 Top N（默认 kinds=crash,anr）
- 过滤 `first_analyzed_at IS NULL`（去重——同一 issue 不重复跑）
- 对每条 issue 调 `start_analysis`，立刻返回 run_id 列表
- 后台 task 跑完后由 analyzer 自己 update DB；这里只在 start_analysis 成功后写一次
  `first_analyzed_at` / `last_analyzed_at`，避免并发跑时重复 schedule

用法：
    result = await batch_analyze_top(top_n=10, force=False)
    # result = {scheduled: [{issue_id, run_id, title}], skipped: [{issue_id, reason}]}
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List

from sqlalchemy import select

from app.crashguard.models import CrashAnalysis, CrashIssue
from app.crashguard.services.analyzer import start_analysis
from app.crashguard.services.ranker import pick_top_n
from app.db.database import get_session

logger = logging.getLogger("crashguard.batch_analyzer")


async def batch_analyze_top(
    top_n: int = 10,
    target_date: date | None = None,
    kinds: tuple = ("crash", "anr"),
    force: bool = False,
) -> Dict[str, Any]:
    """
    取 Top N 中尚未分析过的 issue 批量提交 AI 分析（异步）。

    Args:
        top_n: 取前 N 条
        target_date: 默认今日
        kinds: 类别过滤
        force: True 时即使 first_analyzed_at 已设也重新分析

    Returns:
        {
            "scheduled": [{datadog_issue_id, title, run_id, tier}, ...],
            "skipped":   [{datadog_issue_id, title, reason}, ...],
            "scanned":   N
        }
    """
    if target_date is None:
        target_date = date.today()

    async with get_session() as session:
        top_list = await pick_top_n(
            session,
            today=target_date,
            n=top_n,
            kinds=kinds,
        )
        scheduled: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        if not top_list:
            return {"scheduled": [], "skipped": [], "scanned": 0}

        # 一次取出 top issue 的主表记录
        ids = [t["datadog_issue_id"] for t in top_list]
        rows = (await session.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(ids))
        )).scalars().all()

        # 去重凭证 = 至少一条 status=success 的根因分析（首轮，followup 不算）
        # 失败 / running / empty 都不算"已分析"，下次批量会重试
        success_rows = (await session.execute(
            select(CrashAnalysis.datadog_issue_id).where(
                CrashAnalysis.datadog_issue_id.in_(ids),
                CrashAnalysis.status == "success",
                CrashAnalysis.followup_question == "",
            )
        )).all()
        analyzed_set = {row[0] for row in success_rows}

        # in-flight 防并发：running/pending 也跳过（避免双跑）
        inflight_rows = (await session.execute(
            select(CrashAnalysis.datadog_issue_id).where(
                CrashAnalysis.datadog_issue_id.in_(ids),
                CrashAnalysis.status.in_(["running", "pending"]),
                CrashAnalysis.followup_question == "",
            )
        )).all()
        inflight_set = {row[0] for row in inflight_rows}

        for top in top_list:
            iid = top["datadog_issue_id"]
            title = top.get("title", "")
            if (not force) and iid in analyzed_set:
                skipped.append({
                    "datadog_issue_id": iid,
                    "title": title,
                    "reason": "already_analyzed",
                })
                continue
            if iid in inflight_set:
                skipped.append({
                    "datadog_issue_id": iid,
                    "title": title,
                    "reason": "in_flight",
                })
                continue

            try:
                run_id = await start_analysis(iid, triggered_by="batch")
            except ValueError as exc:
                skipped.append({
                    "datadog_issue_id": iid,
                    "title": title,
                    "reason": f"not_found: {exc}",
                })
                continue
            except Exception as exc:
                logger.exception("batch start_analysis failed for %s", iid)
                skipped.append({
                    "datadog_issue_id": iid,
                    "title": title,
                    "reason": f"start_failed: {exc}",
                })
                continue

            # 标记主表，防止重复 schedule（即使后台 task 还没跑完）
            now = datetime.utcnow()
            row = next((r for r in rows if r.datadog_issue_id == iid), None)
            if row is not None:
                if row.first_analyzed_at is None:
                    row.first_analyzed_at = now
                row.last_analyzed_at = now

            scheduled.append({
                "datadog_issue_id": iid,
                "title": title,
                "run_id": run_id,
                "tier": top.get("tier", "P1"),
            })

        await session.commit()

    logger.info(
        "batch_analyze: scheduled=%d skipped=%d scanned=%d",
        len(scheduled), len(skipped), len(top_list),
    )
    return {
        "scheduled": scheduled,
        "skipped": skipped,
        "scanned": len(top_list),
    }
