"""distribution_prewarmer.py 候选选取单测（2026-07-20）。

背景：`prewarm_today_distributions` 是唯一真正拿到完整堆栈 + binary_images 并调用
symbolicate_stack 的路径（经 get_issue_detail）。但 only_missing 模式下，一旦某
issue 的 prewarm_attempts 达到 _MAX_PREWARM_ATTEMPTS，就永久跳过——即使这个 issue
之后每天都有新事件（102 上实测样本：iOS issue 从 05-29 起卡死在 attempts=3，但
05-29 到 07-20 每天都有 8~408 次新事件，代表性堆栈永远停在 8 字节的占位符）。

修复：candidate 选取时，若 issue.last_seen_at 比 issue.prewarm_last_at 更新，说明
这不是一个"查无可查"的静止 issue，即使重试耗尽也该给它新的尝试机会。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def patched_session(db_engine):
    """复用 conftest db_engine，把全局 _session_factory 指过来（同 test_job_health_alerter.py 模式）。"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401 — 注册 crash_* 表

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original


def _patch_settings(monkeypatch):
    s = MagicMock()
    s.datadog_api_key = "fake-key"
    s.datadog_app_key = "fake-app-key"
    s.datadog_site = "datadoghq.com"
    s.datadog_service_filter = ""
    monkeypatch.setattr(
        "app.crashguard.services.distribution_prewarmer.get_crashguard_settings",
        lambda: s,
    )
    return s


def _patch_always_fail_client(monkeypatch):
    """DatadogClient.get_issue_detail 永远返回 None（模拟"查无事件"失败）。

    我们只关心哪些 issue 被选为 candidate（会触发一次 get_issue_detail 调用并
    bump prewarm_attempts），不关心失败本身的原因。

    注意：只 patch 实例方法 get_issue_detail，不要把整个 DatadogClient 类换成
    MagicMock——_stack_needs_symbolication() 也会用到 DatadogClient 上真实的
    _stack_quality_label 静态方法，换成 MagicMock 会让它跟着失真（返回值不是
    字符串，`in _RAW_STACK_QUALITY_LABELS` 恒为 False，误判"不需要重试"）。
    """
    from app.crashguard.services.datadog_client import DatadogClient
    monkeypatch.setattr(DatadogClient, "get_issue_detail", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_exhausted_issue_with_new_evidence_gets_retried(patched_session, monkeypatch):
    """attempts>=3 但 last_seen_at 比 prewarm_last_at 更新 → 仍应进入候选（新证据豁免）。"""
    from app.db.database import get_session
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.distribution_prewarmer import prewarm_today_distributions

    _patch_settings(monkeypatch)
    _patch_always_fail_client(monkeypatch)

    today = date.today()
    stale_attempt = datetime.utcnow() - timedelta(days=5)
    fresh_event = datetime.utcnow() - timedelta(hours=1)

    async with get_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="issue_recurring", platform="ios", title="App Hang",
            last_seen_at=fresh_event,
            prewarm_attempts=3, prewarm_last_at=stale_attempt, prewarm_last_error="no RUM events in lookback window",
            top_os="",
        ))
        s.add(CrashSnapshot(
            datadog_issue_id="issue_recurring", snapshot_date=today,
            events_count=42, users_affected=3,
        ))
        await s.commit()

    result = await prewarm_today_distributions(today=today, max_issues=30)

    async with get_session() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == "issue_recurring")
        )).scalar_one()

    # 候选被选中 → get_issue_detail 被调用一次 → 失败记录使 attempts 从 3 变成 4
    assert row.prewarm_attempts == 4
    assert result["failed"] == 1
    assert result["prewarmed"] == 0


@pytest.mark.asyncio
async def test_exhausted_stale_issue_stays_skipped(patched_session, monkeypatch):
    """attempts>=3 且 last_seen_at 早于/等于 prewarm_last_at（真正静止）→ 应继续跳过。"""
    from app.db.database import get_session
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.distribution_prewarmer import prewarm_today_distributions

    _patch_settings(monkeypatch)
    _patch_always_fail_client(monkeypatch)

    today = date.today()
    last_attempt = datetime.utcnow() - timedelta(days=1)
    old_event = datetime.utcnow() - timedelta(days=5)  # 早于 prewarm_last_at

    async with get_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="issue_dormant", platform="ios", title="App Hang",
            last_seen_at=old_event,
            prewarm_attempts=3, prewarm_last_at=last_attempt, prewarm_last_error="no RUM events in lookback window",
            top_os="",
        ))
        s.add(CrashSnapshot(
            datadog_issue_id="issue_dormant", snapshot_date=today,
            events_count=1, users_affected=1,
        ))
        await s.commit()

    result = await prewarm_today_distributions(today=today, max_issues=30)

    async with get_session() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == "issue_dormant")
        )).scalar_one()

    # 未被选为候选 → attempts 保持不变，计入 exhausted
    assert row.prewarm_attempts == 3
    assert result["failed"] == 0
    assert result["exhausted"] == 1


@pytest.mark.asyncio
async def test_issue_with_distribution_but_raw_stack_is_still_a_candidate(patched_session, monkeypatch):
    """2026-07-20 修复：has_dist=True(top_os 已有值) 不该等于"已完成"。

    102 实测：一批 iOS ANR issue 的 get_issue_detail 找到了 RUM 事件（分布数据
    写入成功、top_os 有值），但末尾符号化环节撞上 GH_TOKEN 403 静默失败，
    representative_stack 停留在原始地址（"App 0x... + offset"）。旧逻辑只看
    has_dist 就跳过，这批 issue 被永久锁死。has_dist 和"栈是否真的符号化"是
    两回事，不该混为一谈。
    """
    from app.db.database import get_session
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.distribution_prewarmer import prewarm_today_distributions

    _patch_settings(monkeypatch)
    _patch_always_fail_client(monkeypatch)

    today = date.today()
    recent_attempt = datetime.utcnow() - timedelta(hours=1)  # 刚试过，非耗尽也非陈旧

    async with get_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="issue_raw_but_has_dist", platform="ios", title="ANR",
            last_seen_at=datetime.utcnow(),
            prewarm_attempts=1, prewarm_last_at=recent_attempt, prewarm_last_error="",
            top_os="iOS 26.4.2 (100.0%)",  # 分布数据已写入 —— 旧逻辑会因此直接跳过
            representative_stack="0   App   0x0000000112fec700 0x11214c000 + 15337216",
        ))
        s.add(CrashSnapshot(
            datadog_issue_id="issue_raw_but_has_dist", snapshot_date=today,
            events_count=4, users_affected=2,
        ))
        await s.commit()

    result = await prewarm_today_distributions(today=today, max_issues=30)

    # 候选被选中（即便 has_dist=True）→ get_issue_detail 被调用一次 → attempts 1→2
    async with get_session() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == "issue_raw_but_has_dist")
        )).scalar_one()
    assert row.prewarm_attempts == 2
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_issue_with_distribution_and_symbolicated_stack_is_skipped(patched_session, monkeypatch):
    """对照组：has_dist=True 且栈已经真的符号化 → 应继续跳过，不做无意义重试。"""
    from app.db.database import get_session
    from app.crashguard.models import CrashSnapshot, CrashIssue
    from app.crashguard.services.distribution_prewarmer import prewarm_today_distributions

    _patch_settings(monkeypatch)
    _patch_always_fail_client(monkeypatch)

    today = date.today()

    async with get_session() as s:
        s.add(CrashIssue(
            datadog_issue_id="issue_properly_symbolicated", platform="ios", title="ANR",
            last_seen_at=datetime.utcnow(),
            prewarm_attempts=1, prewarm_last_at=datetime.utcnow() - timedelta(hours=1), prewarm_last_error="",
            top_os="iOS 26.4.2 (100.0%)",
            representative_stack="0   App   -[PLRecordManager stopRecording] PLRecordManager.swift:120",
        ))
        s.add(CrashSnapshot(
            datadog_issue_id="issue_properly_symbolicated", snapshot_date=today,
            events_count=4, users_affected=2,
        ))
        await s.commit()

    result = await prewarm_today_distributions(today=today, max_issues=30)

    async with get_session() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(CrashIssue).where(CrashIssue.datadog_issue_id == "issue_properly_symbolicated")
        )).scalar_one()
    assert row.prewarm_attempts == 1  # 未被重跑
    assert result["failed"] == 0
