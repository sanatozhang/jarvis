"""
三维新增分类器：
- is_new_in_version  : 该 issue 的首发版本就是当前最新发布版（"全新"）
- is_regression      : fingerprint 在最近 N 个版本静默后又出现（"回归"）
- is_surge           : 当日事件数环比飙升（"飙升"）
"""
from __future__ import annotations

from typing import List


def is_new_in_version(first_seen_version: str, latest_release: str) -> bool:
    """全新崩溃: 首次出现的版本 == 当前线上最新版"""
    if not first_seen_version or not latest_release:
        return False
    return first_seen_version.strip() == latest_release.strip()


def is_regression(
    fingerprint_seen_versions: List[str],
    recent_versions: List[str],
    current_version: str,
    silent_threshold: int = 3,
) -> bool:
    """
    回归崩溃判定:
    - fingerprint 历史上出现过（fingerprint_seen_versions 非空）
    - 但在最近 silent_threshold 个版本里**完全静默**（recent_versions 与 seen 不相交）
    - 当前版本（current_version）又出现了（这里调用方保证 current_version 命中）
    """
    if not fingerprint_seen_versions:
        return False  # 全新 fingerprint，不算 regression

    if len(recent_versions) < silent_threshold:
        return False  # 历史窗口不足，无法判定

    seen_set = set(fingerprint_seen_versions)
    recent_set = set(recent_versions)

    # 历史出现过 + 最近窗口完全静默 = regression
    if seen_set & recent_set:
        return False  # 最近还出现过，不算静默
    return True


def is_surge(
    today_events: int,
    prev_avg_events: float,
    multiplier: float = 1.5,
    min_events: int = 10,
) -> bool:
    """
    飙升判定:
    - today_events > prev_avg_events * multiplier
    - 且 today_events >= min_events（防小基数刷量）
    - prev_avg_events == 0 时，只要 today_events >= min_events 就算
    """
    if today_events < min_events:
        return False
    if prev_avg_events <= 0:
        return True
    return today_events > prev_avg_events * multiplier


import json
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def classify_today(
    session: AsyncSession,
    today: date,
    latest_release: str,
    recent_versions: List[str],
    surge_multiplier: float = 1.5,
    surge_min_events: int = 10,
    regression_silent_threshold: int = 3,
    surge_baseline_days: int = 7,
) -> None:
    """
    跑完后，crash_snapshots 当天每行的 is_new_in_version / is_regression /
    is_surge 三个 flag 都被填上。
    """
    from app.crashguard.models import CrashSnapshot, CrashIssue, CrashFingerprint

    # 1. 拉今日所有 snapshot + 关联 issue + fingerprint
    today_rows = (await session.execute(
        select(CrashSnapshot).where(CrashSnapshot.snapshot_date == today)
    )).scalars().all()

    if not today_rows:
        return

    issue_ids = [r.datadog_issue_id for r in today_rows]
    issues = (await session.execute(
        select(CrashIssue).where(CrashIssue.datadog_issue_id.in_(issue_ids))
    )).scalars().all()
    issue_by_id = {i.datadog_issue_id: i for i in issues}

    fingerprints = {i.stack_fingerprint for i in issues if i.stack_fingerprint}
    fp_rows = (await session.execute(
        select(CrashFingerprint).where(CrashFingerprint.fingerprint.in_(fingerprints))
    )).scalars().all()
    fp_by_key = {f.fingerprint: f for f in fp_rows}

    # 2. surge 基线: 过去 surge_baseline_days 的 events 平均
    baseline_start = today - timedelta(days=surge_baseline_days)
    baseline_rows = (await session.execute(
        select(CrashSnapshot).where(
            CrashSnapshot.snapshot_date >= baseline_start,
            CrashSnapshot.snapshot_date < today,
        )
    )).scalars().all()
    baseline_by_id: dict = {}
    for b in baseline_rows:
        baseline_by_id.setdefault(b.datadog_issue_id, []).append(b.events_count or 0)

    # 3. 逐条更新 flag
    for snap in today_rows:
        issue = issue_by_id.get(snap.datadog_issue_id)
        if not issue:
            continue

        # is_new_in_version
        snap.is_new_in_version = is_new_in_version(
            first_seen_version=issue.first_seen_version or "",
            latest_release=latest_release,
        )

        # is_regression
        fp_seen_versions: List[str] = []
        if issue.stack_fingerprint and issue.stack_fingerprint in fp_by_key:
            # crash_fingerprints.datadog_issue_ids 是 JSON list, 但版本在 issues 表
            # 简化：取该 fingerprint 关联所有 issue 的 last_seen_version 集合作为 seen
            ids_for_fp = json.loads(fp_by_key[issue.stack_fingerprint].datadog_issue_ids or "[]")
            for related in issues:
                if related.datadog_issue_id in ids_for_fp and related.last_seen_version:
                    fp_seen_versions.append(related.last_seen_version)

        snap.is_regression = is_regression(
            fingerprint_seen_versions=fp_seen_versions,
            recent_versions=recent_versions,
            current_version=latest_release,
            silent_threshold=regression_silent_threshold,
        )

        # is_surge
        history = baseline_by_id.get(snap.datadog_issue_id, [])
        prev_avg = sum(history) / len(history) if history else 0.0
        snap.is_surge = is_surge(
            today_events=snap.events_count or 0,
            prev_avg_events=prev_avg,
            multiplier=surge_multiplier,
            min_events=surge_min_events,
        )
