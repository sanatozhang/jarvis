"""Top20 排序器测试"""
from __future__ import annotations

import pytest


def test_compute_impact_score_basic():
    """impact_score = users_affected × log10(events_count + 1) — 简单分布"""
    from app.crashguard.services.ranker import compute_impact_score

    # 基线: 高用户数 × 中等事件数 → 高分
    score_high = compute_impact_score(users_affected=100, events_count=1000)
    score_low = compute_impact_score(users_affected=5, events_count=10)
    assert score_high > score_low


def test_compute_impact_score_returns_zero_for_empty():
    """无数据时为 0"""
    from app.crashguard.services.ranker import compute_impact_score
    assert compute_impact_score(users_affected=0, events_count=0) == 0.0


def test_compute_impact_score_user_dominated():
    """1 用户崩 1000 次 < 1000 用户各崩 1 次（用户多样性优先）"""
    from app.crashguard.services.ranker import compute_impact_score
    s_one_user = compute_impact_score(users_affected=1, events_count=1000)
    s_many = compute_impact_score(users_affected=1000, events_count=1)
    assert s_many > s_one_user


@pytest.mark.asyncio
async def test_pick_top_n_p0_priority(tmp_path, monkeypatch):
    """P0 (is_new OR is_regression) 强制入选；剩余按 impact_score 排序"""
    from datetime import date
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.ranker import pick_top_n

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rank.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()

    async with get_session() as s:
        # i_p0_new: P0 全新（影响分较低，但必须入选）
        s.add(CrashIssue(datadog_issue_id="i_p0_new", platform="flutter", title="P0 new"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p0_new", snapshot_date=today,
            events_count=5, users_affected=2, crash_free_impact_score=0.6,
            is_new_in_version=True,
        ))
        # i_p0_reg: P0 回归
        s.add(CrashIssue(datadog_issue_id="i_p0_reg", platform="flutter", title="P0 reg"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p0_reg", snapshot_date=today,
            events_count=10, users_affected=3, crash_free_impact_score=3.0,
            is_regression=True,
        ))
        # i_p1_high: P1 高影响
        s.add(CrashIssue(datadog_issue_id="i_p1_high", platform="flutter", title="P1 high"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p1_high", snapshot_date=today,
            events_count=500, users_affected=80, crash_free_impact_score=216.4,
        ))
        # i_p1_low: P1 低影响
        s.add(CrashIssue(datadog_issue_id="i_p1_low", platform="flutter", title="P1 low"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_p1_low", snapshot_date=today,
            events_count=10, users_affected=5, crash_free_impact_score=5.2,
        ))
        await s.commit()

    async with get_session() as s:
        top = await pick_top_n(s, today=today, n=3)
        ids = [t["datadog_issue_id"] for t in top]
        # P0 必须入选（位置不限，但前 2 个肯定有 P0）
        assert "i_p0_new" in ids
        assert "i_p0_reg" in ids
        # 还应有一个 P1 high（影响分最大的 P1）
        assert "i_p1_high" in ids
        # i_p1_low 影响分最低，3 个名额应被前 3 个挤掉
        assert "i_p1_low" not in ids


@pytest.mark.asyncio
async def test_pick_top_n_returns_sorted_by_score_within_tier(tmp_path, monkeypatch):
    """同 tier 内按 impact_score DESC 排"""
    from datetime import date
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.ranker import pick_top_n

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rank2.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()

    async with get_session() as s:
        for i, score in enumerate([1.0, 100.0, 50.0]):
            s.add(CrashIssue(datadog_issue_id=f"x{i}", platform="flutter", title=f"x{i}"))
            s.add(CrashSnapshot(
                datadog_issue_id=f"x{i}", snapshot_date=today,
                crash_free_impact_score=score,
            ))
        await s.commit()

    async with get_session() as s:
        top = await pick_top_n(s, today=today, n=10)
        scores = [t["crash_free_impact_score"] for t in top]
        assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_pick_top_n_skips_recently_reported(tmp_path, monkeypatch):
    """同 issue 7 天内已在某日报里推送过 → 跳过（除非 is_surge）"""
    from datetime import date, timedelta
    import json
    from app.db.database import get_session, init_db
    from app.crashguard import models  # noqa
    from app.crashguard.models import CrashSnapshot, CrashIssue, CrashDailyReport
    from app.crashguard.services.ranker import pick_top_n

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'rank3.db'}")
    from app.config import get_settings
    get_settings.cache_clear()
    await init_db()

    today = date.today()
    five_days_ago = today - timedelta(days=5)

    async with get_session() as s:
        # i_recently_reported: 5 天前已推送，今日普通 P1（应被跳过）
        s.add(CrashIssue(datadog_issue_id="i_dup", platform="flutter", title="dup"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_dup", snapshot_date=today,
            crash_free_impact_score=100.0,
        ))
        # i_dup_surge: 5 天前推过，今日是 surge（应保留）
        s.add(CrashIssue(datadog_issue_id="i_dup_surge", platform="flutter", title="dup surge"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_dup_surge", snapshot_date=today,
            crash_free_impact_score=80.0, is_surge=True,
        ))
        # i_fresh: 全新，未推过
        s.add(CrashIssue(datadog_issue_id="i_fresh", platform="flutter", title="fresh"))
        s.add(CrashSnapshot(
            datadog_issue_id="i_fresh", snapshot_date=today,
            crash_free_impact_score=50.0,
        ))
        # 历史报告记录
        s.add(CrashDailyReport(
            report_date=five_days_ago,
            report_type="morning",
            top_n=2,
            report_payload=json.dumps({
                "issues": [
                    {"datadog_issue_id": "i_dup"},
                    {"datadog_issue_id": "i_dup_surge"},
                ],
            }),
        ))
        await s.commit()

    async with get_session() as s:
        top = await pick_top_n(s, today=today, n=10, dedup_days=7)
        ids = [t["datadog_issue_id"] for t in top]

    assert "i_dup" not in ids          # 7 天内重复 → 跳过
    assert "i_dup_surge" in ids         # surge 例外
    assert "i_fresh" in ids
