"""warmup.py::_collect_attention_ids() ①.6 fatal 兜底通道单测（2026-08-04）。

背景：①.5 只解决了"卡顿(jank)抢不过崩溃"的问题，但反过来低频 fatal 崩溃
（events 个位数）跟高频 fatal/non_fatal 拼 events DESC 排序一样输，导致部分致命崩溃
几个月都进不了自动分析池（生产实测 2026-08-04：18 个 fatal+fixable 从未分析，最早
积压近 6 个月）。本测试验证新增的"①.6 fatal 兜底通道"修复了这个问题。

修复记录（fix round 1，2026-08-04）：最初实现按 CrashIssue.first_analyzed_at IS NULL
判断"从未分析过"，但经代码走查确认，自动 pipeline 路径（analyze_tick /
run_ai_analysis_phase → _auto_analyze_attention → analyzer.analyze_issue）从不回写
这个字段——只有手动 /api/crash/batch-analyze（batch_analyzer.py）和启动时 zombie task
回收（migrations.py）两处会写。这意味着"只走自动流水线分析过、字段没同步"的 issue
会被误判为"从未分析过"，挤占本通道本就稀缺的 3 个名额。

已修复：改为复用批次 1 写好的 `_filter_pending_ids`（真正查 CrashAnalysis 表
success/running/pending 状态）判断"是否真的没有分析记录"，不再依赖
first_analyzed_at 字段。本文件的测试 fixture 相应地改为通过"是否存在 CrashAnalysis
记录"（而不是 first_analyzed_at 列）来构造"已分析过"/"从未分析过"的场景，
`test_analyzed_via_pipeline_but_first_analyzed_at_null_not_reselected` 是专门针对
这次 bug 的回归用例。
"""
from __future__ import annotations

from datetime import date, datetime
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
    s.fatal_backlog_max_slots = 3
    for k, v in overrides.items():
        setattr(s, k, v)
    # get_crashguard_settings 在 warmup.py 里是函数内局部 import，要 patch 源模块
    monkeypatch.setattr(
        "app.crashguard.config.get_crashguard_settings", lambda: s,
    )
    return s


async def _seed_fatal(
    factory, issue_id, events, *,
    kind="crash", fatality="fatal", fixable=True,
    first_seen_at=None, today=None,
    stack="",
    with_success_analysis=False,
):
    """种一个 fatal/anr issue。是否"已分析过"由 with_success_analysis 控制——
    真正插入一条 status="success" 的 CrashAnalysis 记录（phase="fix"），
    跟 _filter_pending_ids 的判断口径一致，不再摆弄 first_analyzed_at 列。
    """
    from app.crashguard.models import CrashIssue, CrashSnapshot, CrashAnalysis
    async with factory() as session:
        session.add(CrashIssue(
            datadog_issue_id=issue_id, title=f"Fatal @ {issue_id}",
            platform="android", kind=kind, fatality=fatality, fixable=fixable,
            first_seen_at=first_seen_at or datetime.utcnow(),
            representative_stack=stack,
        ))
        session.add(CrashSnapshot(datadog_issue_id=issue_id, snapshot_date=today, events_count=events))
        if with_success_analysis:
            session.add(CrashAnalysis(
                datadog_issue_id=issue_id,
                analysis_run_id=f"run-{issue_id}",
                status="success",
                followup_question="",
                phase="fix",
                root_cause="cached root cause",
                created_at=datetime.utcnow(),
            ))
        await session.commit()


async def _seed_big_crash(factory, issue_id, events, today):
    """高 events、已经分析过（真实 CrashAnalysis success 记录）的 fatal issue，
    用来把 ② 里的 Top N 名额占满，同时确保它不会被①.6 误当作"待分析"抢占名额。
    """
    await _seed_fatal(
        factory, issue_id, events,
        first_seen_at=datetime.utcnow(), today=today,
        with_success_analysis=True,
    )


@pytest.mark.asyncio
async def test_never_analyzed_low_event_fatal_gets_priority_slot(patched_session, monkeypatch):
    """ONNX Runtime SIGABRT 场景复现：events=1、从未分析过、first_seen 很早的 fatal crash，
    如果跟一堆高 events 且已分析过的崩溃拼纯 events DESC 排序会被挤出 Top N——
    ①.6 通道必须保证它仍然进入结果。"""
    _patch_settings(monkeypatch)
    today = date(2026, 8, 4)

    # 25 个高流量、已分析过的崩溃，事件数远超新增的低频 fatal，共享 events-DESC 排序
    # 的话 20 个名额（analyze_top_n）会被这些占满。
    for i in range(25):
        await _seed_big_crash(patched_session, f"crash-{i}", events=1000 - i, today=today)

    await _seed_fatal(
        patched_session, "fatal:onnx", events=1, fixable=True,
        first_seen_at=datetime(2026, 2, 13, 6, 0, 0),  # 积压近 6 个月
        today=today,
    )

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "fatal:onnx" in ids


@pytest.mark.asyncio
async def test_fatal_backlog_max_slots_caps_and_orders_by_first_seen_asc(patched_session, monkeypatch):
    """超过 fatal_backlog_max_slots（默认 3）个从未分析过的 fatal issue 时，
    只有 first_seen_at 最早的 3 个进入①.6 通道；其余能否进池取决于②里 events 排序。

    设计：cap（analyze_top_n）设得比 fatal_backlog_max_slots 大很多，且用 10 个高 events
    的 filler 崩溃（已分析过，不会被①.6 抢占名额）把②的名额挤满——这样"只有 3 个
    backlog 进池"是真的由 fatal_backlog_max_slots 决定的，不是巧合撞上了 cap。
    """
    _patch_settings(monkeypatch, analyze_top_n=10, fatal_backlog_max_slots=3)
    today = date(2026, 8, 4)

    # 5 个从未分析过的低频 fatal（events=1），first_seen_at 依次更晚。
    for i in range(5):
        await _seed_fatal(
            patched_session, f"fatal:backlog-{i}", events=1, fixable=True,
            first_seen_at=datetime(2026, 2, 13 + i, 6, 0, 0),
            today=today,
        )
    # 10 个高 events、已分析过的 filler 崩溃，把②仅剩的 7 个名额（cap10 - 已用3）全部
    # 挤满，确保 backlog-3/backlog-4（events=1，垫底）没有机会凭 events 排进②。
    for i in range(10):
        await _seed_big_crash(patched_session, f"filler-{i}", events=1000 - i, today=today)

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)

    assert "fatal:backlog-0" in ids
    assert "fatal:backlog-1" in ids
    assert "fatal:backlog-2" in ids
    assert "fatal:backlog-3" not in ids
    assert "fatal:backlog-4" not in ids
    # 3 个 backlog + 7 个 filler 正好填满 cap=10
    assert len(ids) == 10


@pytest.mark.asyncio
async def test_jank_issue_not_picked_up_by_fatal_channel(patched_session, monkeypatch):
    """kind="jank" 的 issue 即使 fatality 字段被错误标成 fatal，也不应该被①.6 通道选中——
    ①.6 的 kind 过滤 (kind.in_(("crash","anr"))) 必须生效。"""
    _patch_settings(monkeypatch)
    today = date(2026, 8, 4)

    from app.crashguard.models import CrashIssue, CrashSnapshot
    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="jank:mislabeled", title="Jank mislabeled as fatal",
            platform="ios", kind="jank", fatality="fatal", fixable=True,
            first_seen_at=datetime(2026, 2, 13, 6, 0, 0),
        ))
        session.add(CrashSnapshot(datadog_issue_id="jank:mislabeled", snapshot_date=today, events_count=1))
        await session.commit()

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "jank:mislabeled" not in ids


@pytest.mark.asyncio
async def test_already_analyzed_fatal_issue_not_reselected_by_backlog_channel(patched_session, monkeypatch):
    """有 success CrashAnalysis 记录的 fatal issue 不会被①.6 通道选中——它应该走②的
    正常排序，如果 events 不够就落选，这是预期行为（不是 bug）。"""
    _patch_settings(monkeypatch, analyze_top_n=1)
    today = date(2026, 8, 4)

    # 一个高 events、已分析过的崩溃占满①和②仅有的 1 个名额
    await _seed_big_crash(patched_session, "crash-already-analyzed-high-events", events=999, today=today)

    # 一个 events 很低、但已经有 success 分析记录的 fatal issue —— 不该走①.6，
    # 也没有足够 events 走②，所以预期落选。
    await _seed_fatal(
        patched_session, "fatal:already-analyzed-low-events", events=1, fixable=True,
        first_seen_at=datetime(2026, 1, 1, 0, 0, 0),
        today=today,
        with_success_analysis=True,
    )

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "fatal:already-analyzed-low-events" not in ids


@pytest.mark.asyncio
async def test_duplicate_id_already_selected_does_not_waste_backlog_slot(patched_session, monkeypatch):
    """最终修复轮问题 2 回归测试：一个 fatal issue 同时满足①（is_new_in_version）
    和①.6 的准入条件时，①.6 循环遍历到它必须 `continue`（不计入 fatal_backlog_max_slots），
    不能让"重复项"白白吃掉本就稀缺的兜底名额——否则真正只能靠①.6 才够格进池的 issue
    会被挤掉。用 fatal_backlog_max_slots=1 让 bug 必现：修复前，重复项消耗掉唯一的
    1 个名额，genuine 候选连①.6 的门都进不去，又因为 cap already 用满而进不了②；
    修复后，重复项被跳过不消耗名额，genuine 候选顶上①.6 的名额。
    """
    _patch_settings(monkeypatch, analyze_top_n=2, fatal_backlog_max_slots=1)
    today = date(2026, 8, 4)

    from app.crashguard.models import CrashIssue, CrashSnapshot
    async with patched_session() as session:
        # 同时符合①(is_new_in_version) 和 ①.6(fatal+fixable+从未分析) 准入条件的 issue，
        # first_seen_at 最早——①.6 通道按 first_seen_at ASC 排序，它会排在候选队列最前面。
        session.add(CrashIssue(
            datadog_issue_id="fatal:dup-selected-by-new",
            title="Selected by is_new_in_version, also fatal-backlog eligible",
            platform="android", kind="crash", fatality="fatal", fixable=True,
            first_seen_at=datetime(2026, 1, 1, 0, 0, 0),
        ))
        session.add(CrashSnapshot(
            datadog_issue_id="fatal:dup-selected-by-new", snapshot_date=today,
            events_count=5, is_new_in_version=True,
        ))
        await session.commit()

    # 真正只能靠①.6 才进池的 issue：从未分析过、events 很低、first_seen_at 比上面晚。
    await _seed_fatal(
        patched_session, "fatal:genuine-backlog-candidate", events=1, fixable=True,
        first_seen_at=datetime(2026, 1, 2, 0, 0, 0),
        today=today,
    )

    # filler：已分析过、events 很高——如果 genuine 没能靠①.6 拿到名额，②里事件数
    # 排序会让 filler 顶替它挤满剩余的 1 个 cap 名额。
    await _seed_big_crash(patched_session, "crash-filler-high-events", events=999, today=today)

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)

    assert "fatal:dup-selected-by-new" in ids
    assert "fatal:genuine-backlog-candidate" in ids
    assert "crash-filler-high-events" not in ids
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_non_fixable_platform_not_picked_up_by_fatal_channel(patched_session, monkeypatch):
    """最终修复轮问题 3 回归测试：①.6 通道跟①/②一样必须过滤平台白名单——BROWSER/JS
    错误没有对应 mobile repo，分析后无法生成 PR，即使 fatal+fixable+从未分析也不该被
    ①.6 选中（修复前①.6 查询漏了这条过滤，本用例会在修复前失败）。"""
    _patch_settings(monkeypatch)
    today = date(2026, 8, 4)

    from app.crashguard.models import CrashIssue, CrashSnapshot
    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="fatal:browser-not-fixable",
            title="Fatal BROWSER crash, no mobile repo to fix it in",
            platform="BROWSER", kind="crash", fatality="fatal", fixable=True,
            first_seen_at=datetime(2026, 1, 1, 0, 0, 0),
        ))
        session.add(CrashSnapshot(
            datadog_issue_id="fatal:browser-not-fixable", snapshot_date=today, events_count=1,
        ))
        await session.commit()

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "fatal:browser-not-fixable" not in ids


@pytest.mark.asyncio
async def test_analyzed_via_pipeline_but_first_analyzed_at_null_not_reselected(patched_session, monkeypatch):
    """回归测试（fix round 1 的 bug 本身）：一个 fatal issue 的 CrashIssue.first_analyzed_at
    是 NULL（因为它只走过自动流水线分析，从没碰过 /api/crash/batch-analyze，那个字段永远
    不会被自动流水线回写），但它**实际有**一条 status="success" 的 CrashAnalysis 记录
    （模拟真实被分析过的情况）。①.6 通道必须依据 CrashAnalysis 记录判断，不能因为
    first_analyzed_at 恰好是 NULL 就误判为"从未分析过"再次把它选进兜底通道。

    构造上让它 first_seen_at 很早、events 很低——如果 bug 复现（错误依赖
    first_analyzed_at），它会被①.6 优先选中；修复后应该被正确排除。
    """
    _patch_settings(monkeypatch, analyze_top_n=1, fatal_backlog_max_slots=3)
    today = date(2026, 8, 4)

    from app.crashguard.models import CrashIssue, CrashSnapshot, CrashAnalysis
    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="fatal:analyzed-via-pipeline-only",
            title="Analyzed by auto pipeline, first_analyzed_at never synced",
            platform="android", kind="crash", fatality="fatal", fixable=True,
            first_seen_at=datetime(2026, 1, 1, 0, 0, 0),  # 全场最早，若 bug 复现必然抢占①.6
            first_analyzed_at=None,  # 关键：自动流水线从不回写这个字段
        ))
        session.add(CrashSnapshot(
            datadog_issue_id="fatal:analyzed-via-pipeline-only",
            snapshot_date=today, events_count=1,
        ))
        # 真实分析记录：证明它已经被 analyzer.analyze_issue 分析过
        session.add(CrashAnalysis(
            datadog_issue_id="fatal:analyzed-via-pipeline-only",
            analysis_run_id="run-analyzed-via-pipeline-only",
            status="success",
            followup_question="",
            phase="fix",
            root_cause="already analyzed by auto pipeline",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    # 一个高 events 的其它 issue，占掉①和②仅有的 1 个名额（analyze_top_n=1），
    # 确保"fatal:analyzed-via-pipeline-only"只能靠①.6 误选才会进池。
    await _seed_big_crash(patched_session, "crash-filler-high-events", events=999, today=today)

    from app.crashguard.workers.warmup import _collect_attention_ids
    ids = await _collect_attention_ids(today)
    assert "fatal:analyzed-via-pipeline-only" not in ids
