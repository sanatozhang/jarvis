"""三维分类器测试"""
from __future__ import annotations

import pytest


def test_is_new_in_version_true_when_first_seen_matches_latest():
    """issue 的 first_seen_version 等于当前最新发布版 → is_new_in_version=True"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(
        first_seen_version="1.4.7",
        latest_release="1.4.7",
    ) is True


def test_is_new_in_version_false_for_old_issue():
    """老 issue（first_seen_version 早于最新版）→ False"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(
        first_seen_version="1.4.5",
        latest_release="1.4.7",
    ) is False


def test_is_new_in_version_handles_missing():
    """缺数据时返回 False（保守）"""
    from app.crashguard.services.classifier import is_new_in_version

    assert is_new_in_version(first_seen_version="", latest_release="1.4.7") is False
    assert is_new_in_version(first_seen_version="1.4.7", latest_release="") is False


def test_is_regression_when_silent_then_returns():
    """fingerprint 在 v1.4.4 出现，1.4.5/1.4.6/1.4.7 都静默，今日 v1.4.8 又出现 → True"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.4"],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is True


def test_is_regression_false_when_continuously_present():
    """连续出现，从未静默 → False"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.4", "1.4.5", "1.4.6", "1.4.7"],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False


def test_is_regression_false_for_brand_new_fingerprint():
    """全新 fingerprint（之前从未出现）→ 不算 regression（应归为 is_new_in_version）"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=[],
        recent_versions=["1.4.5", "1.4.6", "1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False


def test_is_regression_false_when_silence_too_short():
    """只静默 1 个版本（少于 threshold=3）→ False"""
    from app.crashguard.services.classifier import is_regression

    assert is_regression(
        fingerprint_seen_versions=["1.4.6"],
        recent_versions=["1.4.7"],
        current_version="1.4.8",
        silent_threshold=3,
    ) is False


def test_is_surge_true_when_more_than_multiplier_and_min_events():
    """today=20, prev_avg=10, multiplier=1.5, min_events=10 → 20 > 15 AND 20 >= 10 → True"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=20, prev_avg_events=10,
        multiplier=1.5, min_events=10,
    ) is True


def test_is_surge_false_when_below_multiplier():
    """today=14, prev_avg=10, multiplier=1.5 → 14 < 15 → False"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=14, prev_avg_events=10,
        multiplier=1.5, min_events=10,
    ) is False


def test_is_surge_false_when_below_min_events():
    """today=8, prev_avg=2, ratio=4 但 8 < min_events=10 → False（防小数刷量）"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=8, prev_avg_events=2,
        multiplier=1.5, min_events=10,
    ) is False


def test_is_surge_handles_zero_baseline():
    """prev_avg=0 时，只要超 min_events 就算 surge（无前值，新爆发）"""
    from app.crashguard.services.classifier import is_surge

    assert is_surge(
        today_events=15, prev_avg_events=0,
        multiplier=1.5, min_events=10,
    ) is True

    assert is_surge(
        today_events=5, prev_avg_events=0,
        multiplier=1.5, min_events=10,
    ) is False  # 仍未到 min_events


@pytest.mark.asyncio
async def test_classify_today_writes_three_flags(tmp_path, monkeypatch):
    """classify_today 跑完，crash_snapshots 当天三个 flag 字段都填上"""
    from datetime import date, datetime, timedelta
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue, CrashFingerprint
    from app.crashguard.services.classifier import classify_today

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'cls.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()
    yesterday = today - timedelta(days=1)

    # Issue 1: 全新（first_seen_version == 1.4.7）
    # Issue 2: 飙升（昨日 5 事件，今日 30）— 但需 min_events=10 满足
    # Issue 3: 回归（fingerprint 之前在 1.4.3 出现，最近 1.4.4/5/6 静默，今日 1.4.7 出现）
    async with get_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="i1", stack_fingerprint="fp1", platform="flutter",
            first_seen_version="1.4.7", last_seen_version="1.4.7",
        ))
        s.add(CrashIssue(
            datadog_issue_id="i2", stack_fingerprint="fp2", platform="flutter",
            first_seen_version="1.4.3", last_seen_version="1.4.7",
        ))
        s.add(CrashIssue(
            datadog_issue_id="i3", stack_fingerprint="fp3", platform="flutter",
            first_seen_version="1.4.3", last_seen_version="1.4.7",
        ))
        # 今日 snapshot
        s.add(CrashSnapshot(datadog_issue_id="i1", snapshot_date=today, app_version="1.4.7", events_count=10))
        s.add(CrashSnapshot(datadog_issue_id="i2", snapshot_date=today, app_version="1.4.7", events_count=30))
        s.add(CrashSnapshot(datadog_issue_id="i3", snapshot_date=today, app_version="1.4.7", events_count=15))
        # 昨日 snapshot（用于 surge 计算）
        s.add(CrashSnapshot(datadog_issue_id="i2", snapshot_date=yesterday, app_version="1.4.6", events_count=5))
        # fingerprint 历史
        import json as _json
        s.add(CrashFingerprint(
            fingerprint="fp3",
            datadog_issue_ids=_json.dumps(["i3"]),
            first_seen_version="1.4.3",
        ))
        await s.commit()

    async with get_session() as s:
        await classify_today(
            session=s,
            today=today,
            latest_release="1.4.7",
            recent_versions=["1.4.4", "1.4.5", "1.4.6"],
            surge_multiplier=1.5,
            surge_min_events=10,
            regression_silent_threshold=3,
        )
        await s.commit()

    async with get_session() as s:
        from sqlalchemy import select
        rows = (await s.execute(
            select(CrashSnapshot).where(CrashSnapshot.snapshot_date == today)
        )).scalars().all()
        by_id = {r.datadog_issue_id: r for r in rows}

        assert by_id["i1"].is_new_in_version is True
        assert by_id["i2"].is_surge is True
        assert by_id["i3"].is_regression is True
