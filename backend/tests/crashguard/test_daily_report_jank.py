"""daily_report.py 卡顿(jank) 板块 + 准入阈值单测（2026-07-20）。

背景：daily_report.py 的 attention pool（auto_pr_candidates → attention_issue_ids）
原本完全不按 kind 过滤，只看 fatality + 当日事件数阈值。卡顿量级远小于崩溃，直接复用
现有阈值基本一条都进不去，所以给了独立的 jank_attention_min_events /
jank_daily_new_issue_min_events。同时卡顿(kind='jank')必须完全独立于崩溃报告口径
（不能混进 ## iOS / ## Android 段，也不能被 realtime_today_events 对齐逻辑清零）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.crashguard.models  # noqa: F401


def _make_settings(**overrides):
    base = {
        "datadog_api_key": "",
        "datadog_app_key": "",
        "datadog_site": "datadoghq.com",
        "datadog_window_hours": 24,
        "daily_surge_threshold": 0.10,
        "daily_drop_threshold": -0.10,
        "daily_attention_min_events": 100,
        "frontend_base_url": "http://localhost:3000",
        "feishu_target_chat_id": "",
        "feishu_target_email": "",
        "jank_attention_min_events": 5,
        "jank_daily_new_issue_min_events": 3,
    }
    base.update(overrides)
    return type("S", (), base)()


@pytest.fixture
async def patched_session(db_engine):
    import app.db.database as db_mod

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original_factory = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original_factory


async def _seed(factory, issue_kwargs: dict, snap_kwargs: dict):
    from app.crashguard.models import CrashIssue, CrashSnapshot

    async with factory() as session:
        session.add(CrashIssue(**issue_kwargs))
        session.add(CrashSnapshot(**snap_kwargs))
        await session.commit()


@pytest.mark.asyncio
async def test_new_fixable_symbolicated_jank_enters_attention_and_section(patched_session):
    target = date(2026, 7, 20)
    await _seed(
        patched_session,
        dict(
            datadog_issue_id="jank:abc123", title="Jank @ Plaud-Global",
            platform="ios", kind="jank", fatality="jank", fixable=True,
            first_seen_at=datetime(2026, 7, 20, 6, 0, 0),
            representative_stack="-[PLRecordManager stopRecording] PLRecordManager.swift:120",
        ),
        dict(datadog_issue_id="jank:abc123", snapshot_date=target, events_count=5),
    )

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        text, payload = await daily_report.compose_report("morning", target, top_n=5)

    assert "jank:abc123" in set(payload.get("attention_issue_ids") or [])
    assert "## 🟠 卡顿" in text
    assert "今日新增 1 处" in text


@pytest.mark.asyncio
async def test_unfixable_jank_excluded_from_attention_regardless_of_events(patched_session):
    """has_app_frame=False → fixable=False，即使事件量很高也永久排除。"""
    target = date(2026, 7, 20)
    await _seed(
        patched_session,
        dict(
            datadog_issue_id="jank:sysonly", title="Jank @ QuartzCore",
            platform="ios", kind="jank", fatality="jank", fixable=False,
            first_seen_at=datetime(2026, 7, 20, 6, 0, 0),
            representative_stack="0   QuartzCore   -[CALayer layout] + 12",
        ),
        dict(datadog_issue_id="jank:sysonly", snapshot_date=target, events_count=999),
    )

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        _, payload = await daily_report.compose_report("morning", target, top_n=5)

    assert "jank:sysonly" not in set(payload.get("attention_issue_ids") or [])


@pytest.mark.asyncio
async def test_unsymbolicated_jank_excluded_from_attention(patched_session):
    """fixable=True 但栈还是原始地址（未真正符号化）→ 不进候选（尚不值得自动分析）。"""
    target = date(2026, 7, 20)
    await _seed(
        patched_session,
        dict(
            datadog_issue_id="jank:raw1", title="Jank @ Plaud-Global",
            platform="ios", kind="jank", fatality="jank", fixable=True,
            first_seen_at=datetime(2026, 7, 20, 6, 0, 0),
            representative_stack="Plaud-Global + 0x0000000103e42dd4",  # 符号化失败占位
        ),
        dict(datadog_issue_id="jank:raw1", snapshot_date=target, events_count=50),
    )

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        _, payload = await daily_report.compose_report("morning", target, top_n=5)

    assert "jank:raw1" not in set(payload.get("attention_issue_ids") or [])


@pytest.mark.asyncio
async def test_new_jank_below_threshold_excluded(patched_session):
    """今日新增但事件数低于 jank_daily_new_issue_min_events → 不进候选、不进报告。"""
    target = date(2026, 7, 20)
    await _seed(
        patched_session,
        dict(
            datadog_issue_id="jank:tiny", title="Jank @ Plaud-Global",
            platform="ios", kind="jank", fatality="jank", fixable=True,
            first_seen_at=datetime(2026, 7, 20, 6, 0, 0),
            representative_stack="-[PLRecordManager stopRecording] PLRecordManager.swift:120",
        ),
        dict(datadog_issue_id="jank:tiny", snapshot_date=target, events_count=1),
    )

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        text, payload = await daily_report.compose_report("morning", target, top_n=5)

    assert "jank:tiny" not in set(payload.get("attention_issue_ids") or [])
    assert "## 🟠 卡顿" not in text


@pytest.mark.asyncio
async def test_recurring_jank_uses_attention_threshold_not_new_threshold(patched_session):
    """不是今日新增（first_seen_at 是更早日期）→ 走 jank_attention_min_events（更高）而非
    jank_daily_new_issue_min_events（更低），并归入"持续复现"分组。"""
    target = date(2026, 7, 20)
    await _seed(
        patched_session,
        dict(
            datadog_issue_id="jank:old1", title="Jank @ Plaud-Global",
            platform="ios", kind="jank", fatality="jank", fixable=True,
            first_seen_at=datetime(2026, 6, 1, 6, 0, 0),  # 早于 target_date
            representative_stack="-[PLRecordManager stopRecording] PLRecordManager.swift:120",
        ),
        # 4 满足 new_min(3) 但不满足 attention_min(5)——非今日新增应该用 attention_min 判断，排除
        dict(datadog_issue_id="jank:old1", snapshot_date=target, events_count=4),
    )

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        text, payload = await daily_report.compose_report("morning", target, top_n=5)

    assert "jank:old1" not in set(payload.get("attention_issue_ids") or [])
    assert "## 🟠 卡顿" not in text


@pytest.mark.asyncio
async def test_jank_never_mixed_into_platform_crash_sections(patched_session):
    """卡顿 issue 不能出现在 ## iOS / ## Android 崩溃段——那两段是给 fatal/non_fatal 崩溃用的。"""
    target = date(2026, 7, 20)
    from app.crashguard.models import CrashIssue, CrashSnapshot

    async with patched_session() as session:
        session.add(CrashIssue(
            datadog_issue_id="ios-crash-1", title="iOS Real Crash",
            platform="iOS", fatality="fatal", top_os="iOS 17 (95%)",
            first_seen_version="4.0.0", last_seen_version="4.0.0",
        ))
        session.add(CrashSnapshot(
            datadog_issue_id="ios-crash-1", snapshot_date=target,
            events_count=500, crash_free_impact_score=300.0,
        ))
        session.add(CrashIssue(
            datadog_issue_id="jank:mixed1", title="Jank @ Plaud-Global",
            platform="ios", kind="jank", fatality="jank", fixable=True,
            first_seen_at=datetime(2026, 7, 20, 6, 0, 0),
            representative_stack="-[PLRecordManager stopRecording] PLRecordManager.swift:120",
        ))
        session.add(CrashSnapshot(
            datadog_issue_id="jank:mixed1", snapshot_date=target, events_count=5,
        ))
        await session.commit()

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        text, _ = await daily_report.compose_report("morning", target, top_n=5)

    ios_section_start = text.find("## 🍎 iOS")
    jank_section_start = text.find("## 🟠 卡顿")
    assert ios_section_start > 0
    assert jank_section_start > 0
    # jank 标题本身不应该落在 iOS 段的边界内（下一个 "## " 之前）
    next_heading_after_ios = text.find("\n## ", ios_section_start + 1)
    ios_section_text = text[ios_section_start:next_heading_after_ios] if next_heading_after_ios > 0 else text[ios_section_start:]
    assert "jank:mixed1" not in ios_section_text
    assert "Jank @ Plaud-Global" not in ios_section_text
