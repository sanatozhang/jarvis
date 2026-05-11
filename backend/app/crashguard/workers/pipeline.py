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
from app.crashguard.services.categorizer import classify_kind
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

    # C 路线：双路拉取——fatal 与 non_fatal 分别用独立 query，各自 Top 100 互不挤压。
    # 同一 issue 若两路都返回（理论上不会，因 query 互斥），fatal 优先。
    fatal_raw = await client.list_issues(
        window_hours=s.datadog_window_hours,
        tracks=s.datadog_tracks,
        query=s.datadog_query_fatal,
    )
    nonfatal_raw = await client.list_issues(
        window_hours=s.datadog_window_hours,
        tracks=s.datadog_tracks,
        query=s.datadog_query_nonfatal,
    )
    fatality_by_id: Dict[str, str] = {}
    seen: Dict[str, Dict[str, Any]] = {}
    for item in fatal_raw:
        iid = item.get("id") or ""
        if not iid:
            continue
        seen[iid] = item
        fatality_by_id[iid] = "fatal"
    for item in nonfatal_raw:
        iid = item.get("id") or ""
        if not iid or iid in seen:
            continue  # fatal 优先
        seen[iid] = item
        fatality_by_id[iid] = "non_fatal"
    raw_issues = list(seen.values())
    logger.info(
        "Datadog 双路拉取：fatal=%d / non_fatal=%d / merged=%d (tracks=%s window=%dh)",
        len(fatal_raw), len(nonfatal_raw), len(raw_issues),
        s.datadog_tracks, s.datadog_window_hours,
    )

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

            # Step 3: upsert issues（带 fatality tag）
            await _upsert_issue(
                session, norm, fp,
                fatality=fatality_by_id.get(norm["datadog_issue_id"], "unknown"),
            )

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

    # 后台预热 RUM 分布（fire-and-forget，不阻塞主流程）
    # 给所有今日 snapshot 的 issue 拉 top_os/top_app_version，让早晚报"❓未确定"桶清空
    try:
        import asyncio as _asyncio
        from app.crashguard.services.distribution_prewarmer import prewarm_today_distributions
        _asyncio.create_task(prewarm_today_distributions(today=today, max_issues=30))
        logger.info("prewarmer task scheduled for %s", today)
    except Exception as exc:
        logger.warning("prewarmer schedule failed (non-fatal): %s", exc)

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
    fatality: str = "unknown",
) -> None:
    """upsert crash_issues 主表（含 fatality 分类标签）"""
    from app.crashguard.models import CrashIssue

    row = (await session.execute(
        select(CrashIssue).where(CrashIssue.datadog_issue_id == norm["datadog_issue_id"])
    )).scalar_one_or_none()

    kind = classify_kind(norm["title"], norm.get("platform"), norm.get("service"))

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
            kind=kind,
            fatality=fatality,
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
        row.kind = kind
        # fatality 仅在本次 fetch 提供了明确分类时覆盖（避免历史"unknown"覆盖已分类的行）
        if fatality and fatality != "unknown":
            row.fatality = fatality


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

    sessions = int(norm.get("sessions_affected") or 0)
    score = compute_impact_score(
        users_affected=norm["users_affected"] or sessions,  # 没 user 数据用 sessions 兜底
        events_count=norm["events_count"],
    )

    if row is None:
        row = CrashSnapshot(
            datadog_issue_id=norm["datadog_issue_id"],
            snapshot_date=snapshot_date,
            app_version=norm["last_seen_version"],
            events_count=norm["events_count"],
            users_affected=norm["users_affected"],
            sessions_affected=sessions,
            crash_free_impact_score=score,
        )
        session.add(row)
    else:
        row.events_count = norm["events_count"]
        row.users_affected = norm["users_affected"]
        row.sessions_affected = sessions
        row.crash_free_impact_score = score
        row.app_version = norm["last_seen_version"] or row.app_version
