"""
RUM 分布数据预热器（C 方案）。

目的：让所有今日 snapshot 的 issue 都有 top_os / top_app_version / top_device
分布数据，使早晚报无 "❓ 未确定" 桶、版本主力数据 100% 覆盖。

策略：
- 串行调 Datadog `get_issue_detail`（自带熔断/重试）
- 已有 top_os 的跳过（增量预热）
- 串行而非并发：避免 Datadog rate limit；单 issue ≈ 2-5s，20 条 ≈ 1-2min
- 异步背景跑（asyncio.create_task），不阻塞 pipeline 主流程

入口：
- prewarm_today_distributions(today, max_issues=30) → {prewarmed, skipped, failed}
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Dict

from sqlalchemy import select

from app.crashguard.config import get_crashguard_settings
from app.crashguard.models import CrashIssue, CrashSnapshot
from app.db.database import get_session

logger = logging.getLogger("crashguard.prewarmer")


_MAX_PREWARM_ATTEMPTS = 3  # 单 issue 失败 3 次后不再重试，避免无效循环


async def prewarm_today_distributions(
    today: date,
    max_issues: int = 30,
    only_missing: bool = True,
) -> Dict[str, Any]:
    """
    给今日 snapshot 的 issue（按 events_count desc）拉 RUM 分布写回 crash_issues。

    重试策略：
    - 失败的 issue 计数（prewarm_attempts++）+ 错误原因写回 DB
    - 失败次数 >= _MAX_PREWARM_ATTEMPTS 的 issue 自动跳过（only_missing=True 模式下）
    - 全量刷新（only_missing=False）忽略失败计数

    Args:
        today: 目标日期（一般为 date.today()）
        max_issues: 最多预热多少个（防止打爆 Datadog quota）
        only_missing: True 时仅处理 top_os 为空 + 重试次数未上限的 issue

    Returns:
        {"prewarmed": int, "skipped": int, "failed": int, "scanned": int, "exhausted": int}
    """
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        logger.info("prewarmer skipped: datadog_api_key 未配置")
        return {"prewarmed": 0, "skipped": 0, "failed": 0, "scanned": 0}

    # 1. 取今日所有 snapshot 对应的 issue，按 events_count desc 排序
    async with get_session() as session:
        rows = (await session.execute(
            select(CrashSnapshot, CrashIssue)
            .join(CrashIssue, CrashIssue.datadog_issue_id == CrashSnapshot.datadog_issue_id)
            .where(CrashSnapshot.snapshot_date == today)
            .order_by(CrashSnapshot.events_count.desc())
        )).all()

    candidates = []
    exhausted = 0
    for snap, issue in rows:
        has_dist = bool((getattr(issue, "top_os", "") or "").strip())
        attempts = int(getattr(issue, "prewarm_attempts", 0) or 0)
        if only_missing:
            if has_dist:
                continue
            if attempts >= _MAX_PREWARM_ATTEMPTS:
                exhausted += 1
                continue
        candidates.append(issue.datadog_issue_id)
        if len(candidates) >= max_issues:
            break

    if not candidates:
        logger.info("prewarmer: no candidates (all have top_os or exhausted retries)")
        return {
            "prewarmed": 0, "skipped": len(rows) - exhausted, "failed": 0,
            "scanned": len(rows), "exhausted": exhausted,
        }

    # 2. 串行调 get_issue_detail
    from app.crashguard.services.analyzer import _persist_distribution_to_issue
    from app.crashguard.services.datadog_client import DatadogClient

    client = DatadogClient(
        api_key=s.datadog_api_key,
        app_key=s.datadog_app_key,
        site=s.datadog_site,
    )

    from datetime import datetime as _dt

    async def _record_failure(iid: str, reason: str) -> None:
        async with get_session() as session:
            row = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == iid)
            )).scalar_one_or_none()
            if row is None:
                return
            row.prewarm_attempts = int(getattr(row, "prewarm_attempts", 0) or 0) + 1
            row.prewarm_last_error = (reason or "")[:500]
            row.prewarm_last_at = _dt.utcnow()
            await session.commit()

    async def _record_success(iid: str) -> None:
        async with get_session() as session:
            row = (await session.execute(
                select(CrashIssue).where(CrashIssue.datadog_issue_id == iid)
            )).scalar_one_or_none()
            if row is None:
                return
            row.prewarm_attempts = int(getattr(row, "prewarm_attempts", 0) or 0) + 1
            row.prewarm_last_error = ""
            row.prewarm_last_at = _dt.utcnow()
            await session.commit()

    prewarmed = failed = 0
    for issue_id in candidates:
        try:
            detail = await client.get_issue_detail(issue_id)
        except Exception as exc:
            err = f"get_issue_detail: {type(exc).__name__}: {exc}"
            logger.warning("prewarmer %s failed: %s", issue_id, err)
            await _record_failure(issue_id, err)
            failed += 1
            await asyncio.sleep(0.5)
            continue
        if not detail:
            await _record_failure(issue_id, "no RUM events in lookback window")
            failed += 1
            await asyncio.sleep(0.2)
            continue
        try:
            await _persist_distribution_to_issue(issue_id, detail)
            await _record_success(issue_id)
            prewarmed += 1
        except Exception as exc:
            err = f"persist: {type(exc).__name__}: {exc}"
            logger.warning("prewarmer persist failed for %s: %s", issue_id, err)
            await _record_failure(issue_id, err)
            failed += 1
        # 节流：避免击穿 rate limit
        await asyncio.sleep(0.2)

    logger.info(
        "prewarmer done: prewarmed=%d failed=%d exhausted=%d (out of %d candidates from %d scanned)",
        prewarmed, failed, exhausted, len(candidates), len(rows),
    )
    try:
        from app.crashguard.services.audit import write_audit
        await write_audit(
            op="prewarm",
            target_id=today.isoformat(),
            success=(failed == 0),
            detail={
                "prewarmed": prewarmed, "failed": failed, "exhausted": exhausted,
                "candidates": len(candidates), "scanned": len(rows),
            },
        )
    except Exception:
        pass
    return {
        "prewarmed": prewarmed,
        "skipped": len(rows) - len(candidates) - exhausted,
        "failed": failed,
        "scanned": len(rows),
        "exhausted": exhausted,
    }
