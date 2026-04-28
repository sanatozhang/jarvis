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
