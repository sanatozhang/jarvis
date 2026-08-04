"""`run_ai_analysis_phase` 分片回归测试（2026-08-04）。

背景：`run_ai_analysis_phase`（被每 4 小时一次的 pipeline_scheduler_loop 和启动时的
warmup 调用）之前把**整个** attention_ids 池子直接扔给 `_auto_analyze_attention` 串行
分析。`pipeline_scheduler_loop` 是单线程 `while True` 循环，池子越大，这个入口单次运行
时长越长，会连带卡住同一循环体里排在后面的 pr_reviewer_daily / pr_pending_review 提醒
检查。修复：先用 `_filter_pending_ids` 过滤掉已 success/running/pending 的，再按
`pipeline_analyze_max_per_run` 切片，保证单次运行时长有上限。

`_backfill_attention_auto_pr` 不切片——它只处理"已有 success 分析但没 PR"的子集
（走 PR 草拟而非重新分析），必须仍拿到完整 attention_ids。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_auto_analyze_attention_sliced_to_max_per_run(monkeypatch):
    """池子里有 12 个待分析 issue，pipeline_analyze_max_per_run=5 时，
    `_auto_analyze_attention` 只应该被喂进前 5 个（过滤后按顺序切片）。"""
    from app.crashguard.workers import warmup

    full_pool = [f"issue-{i}" for i in range(12)]

    s_mock = MagicMock()
    s_mock.pipeline_analyze_max_per_run = 5
    monkeypatch.setattr("app.crashguard.config.get_crashguard_settings", lambda: s_mock)

    backfill_mock = AsyncMock(return_value={
        "scanned": 0, "attempted": 0, "created": 0, "skipped": 0, "failed": [],
    })
    monkeypatch.setattr(warmup, "_backfill_attention_auto_pr", backfill_mock)

    with patch(
        "app.crashguard.workers.warmup._collect_attention_ids",
        new=AsyncMock(return_value=full_pool),
    ), patch(
        "app.crashguard.services.daily_report._filter_pending_ids",
        new=AsyncMock(return_value=full_pool),  # 全部待分析，未过滤掉任何一个
    ), patch(
        "app.crashguard.services.daily_report._auto_analyze_attention",
        new=AsyncMock(return_value=5),
    ) as mock_auto_analyze:
        result = await warmup.run_ai_analysis_phase(today=date(2026, 8, 4), reason="test")

    mock_auto_analyze.assert_awaited_once_with(full_pool[:5])
    assert result["attention_count"] == 12
    assert result["analyzed"] == 5


@pytest.mark.asyncio
async def test_backfill_attention_auto_pr_receives_full_unsliced_pool(monkeypatch):
    """`_backfill_attention_auto_pr` 必须拿到完整的 attention_ids（未按
    pipeline_analyze_max_per_run 切片）——它处理的是已完成分析、只是没开 PR 的子集，
    不应该被这次分片误伤。"""
    from app.crashguard.workers import warmup

    full_pool = [f"issue-{i}" for i in range(12)]
    pending_subset = full_pool[:3]  # 假设大部分已经分析过，只剩 3 个待分析

    s_mock = MagicMock()
    s_mock.pipeline_analyze_max_per_run = 5
    monkeypatch.setattr("app.crashguard.config.get_crashguard_settings", lambda: s_mock)

    backfill_mock = AsyncMock(return_value={
        "scanned": 0, "attempted": 0, "created": 0, "skipped": 0, "failed": [],
    })
    monkeypatch.setattr(warmup, "_backfill_attention_auto_pr", backfill_mock)

    with patch(
        "app.crashguard.workers.warmup._collect_attention_ids",
        new=AsyncMock(return_value=full_pool),
    ), patch(
        "app.crashguard.services.daily_report._filter_pending_ids",
        new=AsyncMock(return_value=pending_subset),
    ), patch(
        "app.crashguard.services.daily_report._auto_analyze_attention",
        new=AsyncMock(return_value=3),
    ):
        await warmup.run_ai_analysis_phase(today=date(2026, 8, 4), reason="test")

    # 完整 12 个，不是过滤/切片后的 3 个
    backfill_mock.assert_awaited_once_with(full_pool)


@pytest.mark.asyncio
async def test_no_analyze_call_when_attention_pool_empty(monkeypatch):
    """attention_ids 为空时，不应该调用 backfill / auto_analyze，直接返回全零结果。"""
    from app.crashguard.workers import warmup

    s_mock = MagicMock()
    s_mock.pipeline_analyze_max_per_run = 5
    monkeypatch.setattr("app.crashguard.config.get_crashguard_settings", lambda: s_mock)

    backfill_mock = AsyncMock()
    monkeypatch.setattr(warmup, "_backfill_attention_auto_pr", backfill_mock)

    with patch(
        "app.crashguard.workers.warmup._collect_attention_ids",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.crashguard.services.daily_report._filter_pending_ids",
        new=AsyncMock(),
    ) as mock_filter, patch(
        "app.crashguard.services.daily_report._auto_analyze_attention",
        new=AsyncMock(),
    ) as mock_auto_analyze:
        result = await warmup.run_ai_analysis_phase(today=date(2026, 8, 4), reason="test")

    backfill_mock.assert_not_called()
    mock_filter.assert_not_called()
    mock_auto_analyze.assert_not_called()
    assert result["attention_count"] == 0
    assert result["analyzed"] == 0
