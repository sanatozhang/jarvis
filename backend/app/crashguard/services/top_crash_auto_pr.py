"""Top crash 专属自动 PR 触发器。

底层逻辑：全局 `feasibility_pr_threshold` 默认 0.7 比较保守，会把许多 Top crash
（feasibility 0.5~0.7 区间，本身可以修但 agent 信心不满）拦在自动 PR 门外。

抓手：Top N（按 total_events）享受**专属低门槛 + cron 兜底**——
- 信心 ≥ top_crash_auto_pr_threshold（默认 0.5）的 Top crash 自动开 PR
- 跳过已有 open/draft/merged PR 的 issue（不重复）
- 默认跳过已有 closed PR 的 issue（防 spam，可配置开 retry）
- 每 tick 最多开 max_per_tick 个 PR（节流）

复用 pr_drafter.draft_prs_multi（同 Gate#1-13 闸门），不绕过质量防线。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import desc, or_, select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashAnalysis, CrashIssue, CrashPullRequest
from app.db.database import get_session

logger = logging.getLogger("crashguard.top_crash_auto_pr")


async def run_top_crash_auto_pr_tick() -> Dict[str, Any]:
    """单 tick 主流程：扫 Top N → 按门槛 + 节流派 PR。

    返回 {actioned, pr_urls, skipped, total_scanned}，供 heartbeat summary。
    """
    s = get_crashguard_settings()
    if not getattr(s, "top_crash_auto_pr_enabled", False):
        return {"skipped_reason": "kill_switch_off", "actioned": 0, "pr_urls": []}
    if not getattr(s, "pr_enabled", True):
        return {"skipped_reason": "pr_enabled_off", "actioned": 0, "pr_urls": []}

    top_n = int(getattr(s, "top_crash_auto_pr_top_n", 20) or 20)
    threshold = float(getattr(s, "top_crash_auto_pr_threshold", 0.5) or 0.5)
    max_per_tick = int(getattr(s, "top_crash_auto_pr_max_per_tick", 3) or 3)
    retry_on_closed = bool(getattr(s, "top_crash_auto_pr_retry_on_closed", False))

    actioned = 0
    pr_urls: list[str] = []
    skipped: list[str] = []
    scanned = 0

    async with get_session() as session:
        # Top N crash by total_events（kind=crash 或留空兼容历史）
        issues = (await session.execute(
            select(CrashIssue)
            .where(or_(CrashIssue.kind == "crash", CrashIssue.kind.is_(None)))
            .order_by(desc(CrashIssue.total_events))
            .limit(top_n)
        )).scalars().all()

        for iss in issues:
            scanned += 1
            if actioned >= max_per_tick:
                skipped.append(f"{iss.id}:max_per_tick_reached")
                break

            # 1. 已有 open/draft/merged PR → 跳（不重复）
            prs = (await session.execute(
                select(CrashPullRequest)
                .where(CrashPullRequest.datadog_issue_id == iss.datadog_issue_id)
                .order_by(desc(CrashPullRequest.created_at))
            )).scalars().all()
            has_active = any(
                (p.pr_status or "").lower() in ("open", "draft", "merged")
                for p in prs
            )
            if has_active:
                skipped.append(f"{iss.id}:has_active_pr")
                continue

            # 2. 仅有 closed/ci_failed_closed PR + 未开 retry → 跳
            has_closed = any(
                (p.pr_status or "").lower() in ("closed", "ci_failed_closed")
                for p in prs
            )
            if has_closed and not retry_on_closed:
                skipped.append(f"{iss.id}:has_closed_no_retry")
                continue

            # 3. 必须有 success analysis
            ana = (await session.execute(
                select(CrashAnalysis)
                .where(
                    CrashAnalysis.datadog_issue_id == iss.datadog_issue_id,
                    CrashAnalysis.status == "success",
                    CrashAnalysis.followup_question == "",
                )
                .order_by(desc(CrashAnalysis.id))
                .limit(1)
            )).scalar_one_or_none()
            if ana is None:
                skipped.append(f"{iss.id}:no_success_analysis")
                continue

            # 4. feasibility ≥ Top 专属阈值
            feas = float(ana.feasibility_score or 0.0)
            if feas < threshold:
                skipped.append(f"{iss.id}:fea_{feas:.2f}_lt_{threshold:.2f}")
                continue

            # 5. 触发 PR（复用 draft_prs_multi，含 Gate#1-13）
            try:
                from app.crashguard.services.pr_drafter import draft_prs_multi
                res = await draft_prs_multi(int(ana.id), approver="top_auto")
                if res.get("ok"):
                    actioned += 1
                    urls = [
                        p.get("pr_url") for p in res.get("prs", []) or []
                        if p.get("pr_url")
                    ]
                    pr_urls.extend(urls)
                    logger.info(
                        "top_crash_auto_pr: opened PR for issue %s (ana=%d, fea=%.2f) → %s",
                        iss.datadog_issue_id, ana.id, feas, urls,
                    )
                else:
                    err = "; ".join(
                        (p.get("error") or "") for p in res.get("prs", []) or []
                        if not p.get("ok")
                    )[:120]
                    skipped.append(f"{iss.id}:pr_failed:{err}")
            except Exception as exc:
                logger.exception(
                    "top_crash_auto_pr: exception for issue %s ana=%s",
                    iss.datadog_issue_id, ana.id,
                )
                skipped.append(f"{iss.id}:exc_{type(exc).__name__}")

    return {
        "actioned": actioned,
        "pr_urls": pr_urls,
        "skipped": skipped[:30],          # 防 summary 爆炸
        "skipped_total": len(skipped),
        "total_scanned": scanned,
    }
