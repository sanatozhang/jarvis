"""warmup.py::_collect_attention_ids() 卡顿(jank) 准入单测（2026-07-20）。

背景：这是驱动"每 5 分钟自动分析 tick"和"启动 warmup AI 阶段"的真正入口（跟
daily_report.py::compose_report 的 attention pool 是两套完全独立的计算，后者只用于
早晚报渲染 + 隔天回溯，不驱动日常持续的自动分析）。

实测问题：这里给"fatal+non_fatal 合并"用的是纯 events DESC 排序，卡顿事件量级
天生远小于崩溃（个位数~几十 events vs 崩溃动辄几百上千），混在一起按数字竞争
永远抢不到 analyze_top_n(默认20)的名额——即使代码本身没有按 kind 过滤掉卡顿，
实际效果等同于卡顿永远不会被自动分析/开 PR。本测试验证新增的"①.5 卡顿专属准入"
分支修复了这个问题。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _patch_settings(monkeypatch, **overrides):
    s = MagicMock()
    s.analyze_top_n = 20
    s.auto_pr_fixable_platforms = ["android", "ios", "flutter"]
    s.jank_attention_min_events = 5
    s.jank_daily_new_issue_min_events = 3
    for k, v in overrides.items():
        setattr(s, k, v)
    # get_crashguard_settings 在 warmup.py 里是函数内局部 import，要 patch 源模块
    monkeypatch.setattr(
        "app.crashguard.config.get_crashguard_settings", lambda: s,
    )
    return s


async def _seed_jank(factory, issue_id, events, *, fixable=True, stack="", first_seen_at=None, today=None):
    from app.crashguard.models import CrashIssue, CrashSnapshot
    async with factory() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title=f"Jank @ {issue_id}",
            platform="ios", kind="jank", fatality="jank", fixable=fixable,
            first_seen_at=first_seen_at or datetime.utcnow(),
            representative_stack=stack,
        ))
        session.add(CrashSnapshot(datadog_issue_id=issue_id, snapshot_date=today, events_count=events))
        await session.commit()


async def _seed_big_crash(factory, issue_id, events, today):
    from app.crashguard.models import CrashIssue, CrashSnapshot
    async with factory() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title="Real crash", platform="ios", fatality="fatal",
        ))
        session.add(CrashSnapshot(datadog_issue_id=issue_id, snapshot_date=today, events_count=events))
        await session.commit()


@pytest.mark.asyncio
async def test_qualifying_jank_gets_priority_slot_despite_low_event_count(patched_session, monkeypatch):
    """8-events 的卡顿如果跟几百 events 的真崩溃按纯 events DESC 排是永远选不上的——
    必须走独立的保底入选分支，不跟崩溃拼名额排序。"""
    _patch_settings(monkeypatch)
    today = date(2026, 7, 20)

    # 20 个大流量崩溃，事件数远超卡顿，如果共享同一套 events-DESC 排序会把 20 个名额占满
    for i in range(25):
        await _seed_big_crash(patched_session, f"crash-{i}", events=1000 - i, today=today)

    await _seed_jank(
        patched_session, "jank:1", events=8, fixable=True,
        stack="-[PLRecordManager stopRecording] PLRecordManager.swift:120",
        first_seen_at=datetime(2026, 7, 20, 6, 0, 0), today=today,
    )

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "jank:1" in ids


@pytest.mark.asyncio
async def test_unfixable_jank_never_selected(patched_session, monkeypatch):
    _patch_settings(monkeypatch)
    today = date(2026, 7, 20)
    await _seed_jank(
        patched_session, "jank:sysonly", events=999, fixable=False,
        stack="0   QuartzCore   -[CALayer layout] + 12",
        first_seen_at=datetime(2026, 7, 20, 6, 0, 0), today=today,
    )

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "jank:sysonly" not in ids


@pytest.mark.asyncio
async def test_unsymbolicated_jank_never_selected(patched_session, monkeypatch):
    _patch_settings(monkeypatch)
    today = date(2026, 7, 20)
    await _seed_jank(
        patched_session, "jank:raw", events=999, fixable=True,
        stack="Plaud-Global + 0x0000000103e42dd4",  # 符号化失败占位
        first_seen_at=datetime(2026, 7, 20, 6, 0, 0), today=today,
    )

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "jank:raw" not in ids


@pytest.mark.asyncio
async def test_jank_below_threshold_never_selected(patched_session, monkeypatch):
    _patch_settings(monkeypatch)
    today = date(2026, 7, 20)
    await _seed_jank(
        patched_session, "jank:tiny", events=1, fixable=True,
        stack="-[PLRecordManager stopRecording] PLRecordManager.swift:120",
        first_seen_at=datetime(2026, 7, 20, 6, 0, 0), today=today,
    )

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "jank:tiny" not in ids


@pytest.mark.asyncio
async def test_recurring_jank_uses_higher_attention_threshold(patched_session, monkeypatch):
    """不是今日新增 → 用 jank_attention_min_events(5)，不是 jank_daily_new_issue_min_events(3)。"""
    _patch_settings(monkeypatch)
    today = date(2026, 7, 20)
    await _seed_jank(
        patched_session, "jank:old", events=4, fixable=True,
        stack="-[PLRecordManager stopRecording] PLRecordManager.swift:120",
        first_seen_at=datetime(2026, 6, 1, 6, 0, 0),  # 早于 today，非今日新增
        today=today,
    )

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "jank:old" not in ids  # 4 满足 new_min(3) 但不满足 attention_min(5)
