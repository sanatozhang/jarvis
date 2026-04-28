"""
Crashguard 端到端流水线 — 数据阶段（Step 1-6）。

不含 AI 分析（Step 7+）—— 由 Plan 2 实现。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crashguard.config import get_crashguard_settings
from app.crashguard.services.classifier import classify_today
from app.crashguard.services.datadog_client import DatadogClient, normalize_issue
from app.crashguard.services.dedup import compute_fingerprint, normalize_stack_frames, upsert_fingerprint_link
from app.crashguard.services.ranker import compute_impact_score, pick_top_n
from app.db.database import get_session

logger = logging.getLogger("crashguard.pipeline")


async def run_data_phase(
    today: date,
    latest_release: str,
    recent_versions: List[str],
) -> Dict[str, Any]:
    """
    Step 1-6 数据阶段：
    1. 拉 Datadog issue（24h 窗口）
    2. 计算 stack_fingerprint
    3. Upsert crash_issues 主表
    4. Upsert crash_snapshots 当日快照（含 impact_score）
    5. 跑 classify_today 三维分类
    6. pick_top_n 选 Top20

    返回:
    {
        "issues_processed": int,
        "snapshots_written": int,
        "top_n_count": int,
        "top_n": [...],
    }
    """
    s = get_crashguard_settings()
    if not s.datadog_api_key:
        logger.warning("CRASHGUARD_DATADOG_API_KEY 未配置，pipeline 跳过")
        return {"issues_processed": 0, "snapshots_written": 0, "top_n_count": 0, "top_n": []}

    client = DatadogClient(
        api_key=s.datadog_api_key,
        app_key=s.datadog_app_key,
        site=s.datadog_site,
    )
    raw_issues = await client.list_issues(window_hours=s.datadog_window_hours)
    logger.info("Datadog 拉取 %d 条 issue", len(raw_issues))

    issues_processed = 0
    snapshots_written = 0

    async with get_session() as session:
        for raw in raw_issues:
            norm = normalize_issue(raw)
            if not norm["datadog_issue_id"]:
                continue

            # Step 2: fingerprint
            fp = compute_fingerprint(norm["stack_trace"])
            top_frames = normalize_stack_frames(norm["stack_trace"])

            # Step 3: upsert issues
            await _upsert_issue(session, norm, fp)

            # 关联 fingerprint 表
            await upsert_fingerprint_link(
                session=session,
                fingerprint=fp,
                datadog_issue_id=norm["datadog_issue_id"],
                first_seen_version=norm["first_seen_version"],
                events_count=norm["events_count"],
                normalized_top_frames=top_frames,
            )

            # Step 4: upsert snapshot
            await _upsert_snapshot(session, today, norm)

            issues_processed += 1
            snapshots_written += 1

        await session.commit()

        # Step 5: 三维分类
        await classify_today(
            session=session,
            today=today,
            latest_release=latest_release,
            recent_versions=recent_versions,
            surge_multiplier=s.surge_multiplier,
            surge_min_events=s.surge_min_events,
            regression_silent_threshold=s.regression_silent_versions,
        )
        await session.commit()

        # Step 6: 选 Top N
        top = await pick_top_n(session, today=today, n=s.max_top_n)

    logger.info(
        "pipeline data phase done: issues=%d snapshots=%d top_n=%d",
        issues_processed, snapshots_written, len(top),
    )
    return {
        "issues_processed": issues_processed,
        "snapshots_written": snapshots_written,
        "top_n_count": len(top),
        "top_n": top,
    }


async def _upsert_issue(
    session: AsyncSession,
    norm: Dict[str, Any],
    stack_fingerprint: str,
) -> None:
    """upsert crash_issues 主表"""
    from app.crashguard.models import CrashIssue

    row = (await session.execute(
        select(CrashIssue).where(CrashIssue.datadog_issue_id == norm["datadog_issue_id"])
    )).scalar_one_or_none()

    if row is None:
        row = CrashIssue(
            datadog_issue_id=norm["datadog_issue_id"],
            stack_fingerprint=stack_fingerprint,
            title=norm["title"],
            platform=norm["platform"],
            service=norm["service"],
            first_seen_at=norm["first_seen_at"],
            first_seen_version=norm["first_seen_version"],
            last_seen_at=norm["last_seen_at"],
            last_seen_version=norm["last_seen_version"],
            total_events=norm["events_count"],
            total_users_affected=norm["users_affected"],
            representative_stack=norm["stack_trace"][:8000],  # 限长
            tags=json.dumps(norm["tags"]),
        )
        session.add(row)
    else:
        row.stack_fingerprint = stack_fingerprint
        row.title = norm["title"] or row.title
        row.last_seen_at = norm["last_seen_at"] or row.last_seen_at
        row.last_seen_version = norm["last_seen_version"] or row.last_seen_version
        row.total_events = max(row.total_events or 0, norm["events_count"])
        row.total_users_affected = max(row.total_users_affected or 0, norm["users_affected"])
        if not row.representative_stack:
            row.representative_stack = norm["stack_trace"][:8000]


async def _upsert_snapshot(
    session: AsyncSession,
    snapshot_date: date,
    norm: Dict[str, Any],
) -> None:
    """upsert crash_snapshots 当日行（impact_score 一并算）"""
    from app.crashguard.models import CrashSnapshot

    row = (await session.execute(
        select(CrashSnapshot).where(
            CrashSnapshot.datadog_issue_id == norm["datadog_issue_id"],
            CrashSnapshot.snapshot_date == snapshot_date,
        )
    )).scalar_one_or_none()

    score = compute_impact_score(
        users_affected=norm["users_affected"],
        events_count=norm["events_count"],
    )

    if row is None:
        row = CrashSnapshot(
            datadog_issue_id=norm["datadog_issue_id"],
            snapshot_date=snapshot_date,
            app_version=norm["last_seen_version"],
            events_count=norm["events_count"],
            users_affected=norm["users_affected"],
            crash_free_impact_score=score,
        )
        session.add(row)
    else:
        row.events_count = norm["events_count"]
        row.users_affected = norm["users_affected"]
        row.crash_free_impact_score = score
        row.app_version = norm["last_seen_version"] or row.app_version
