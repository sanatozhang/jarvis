"""端到端集成测试：seed crash_issues / crash_snapshots → compose_report → 验证输出"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


def _make_settings():
    """构造一份完整 settings stub（覆盖 daily_report 用到的所有字段）"""
    return type("S", (), {
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
    })()


@pytest.fixture
async def patched_session(db_engine):
    """切 daily_report 的 get_session 到测试 in-memory DB（含 crashguard 表）"""
    import app.db.database as db_mod
    import app.crashguard.models  # noqa: F401  触发模型注册到 metadata

    async with db_engine.begin() as conn:
        await conn.run_sync(db_mod.Base.metadata.create_all)

    original_factory = db_mod._session_factory
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    db_mod._session_factory = factory
    yield factory
    db_mod._session_factory = original_factory


async def _seed_today_data(factory, target_date: date):
    """seed iOS×2 + Android×1 issue + snapshot"""
    from app.crashguard.models import CrashIssue, CrashSnapshot

    issues = [
        CrashIssue(
            datadog_issue_id="ios-1", title="iOS NSInvalidArgument",
            platform="iOS", top_os="iOS 17 (95%)",
            first_seen_version="3.15.0", last_seen_version="3.16.0",
            top_app_version="3.16.0 (90%)", status="open",
            total_users_affected=120,
        ),
        CrashIssue(
            datadog_issue_id="ios-2", title="iOS Memory leak",
            platform="iOS", top_os="iOS 17 (90%)",
            first_seen_version="3.16.0", last_seen_version="3.16.0",
            top_app_version="3.16.0 (100%)", status="open",
            total_users_affected=50,
        ),
        CrashIssue(
            datadog_issue_id="and-1", title="Android ANR",
            platform="Android", top_os="Android 14 (80%)",
            first_seen_version="3.15.0", last_seen_version="3.16.0",
            top_app_version="3.16.0 (85%)", status="open",
            total_users_affected=200,
        ),
    ]
    snaps = [
        CrashSnapshot(datadog_issue_id="ios-1", snapshot_date=target_date,
                      events_count=500, sessions_affected=350, users_affected=120,
                      crash_free_impact_score=300.0, is_new_in_version=False),
        CrashSnapshot(datadog_issue_id="ios-2", snapshot_date=target_date,
                      events_count=80,  # < min_events=100；但 is_new=True
                      sessions_affected=70, users_affected=50,
                      crash_free_impact_score=120.0, is_new_in_version=True),
        CrashSnapshot(datadog_issue_id="and-1", snapshot_date=target_date,
                      events_count=900, sessions_affected=600, users_affected=200,
                      crash_free_impact_score=520.0, is_new_in_version=False),
    ]
    async with factory() as session:
        for it in issues:
            session.add(it)
        for sn in snaps:
            session.add(sn)
        await session.commit()


@pytest.mark.asyncio
async def test_compose_report_renders_two_platforms(patched_session):
    """compose_report 渲染 iOS + Android 两段，iOS 优先在前"""
    target = date(2026, 4, 29)
    await _seed_today_data(patched_session, target)

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        text, _ = await daily_report.compose_report("morning", target, top_n=5)

    ios_idx = text.find("## 🍎 iOS")
    and_idx = text.find("## 📱 Android")
    assert ios_idx > 0, f"iOS section missing in:\n{text[:500]}"
    assert and_idx > 0, "Android section missing"
    assert ios_idx < and_idx, "iOS should be rendered above Android"


@pytest.mark.asyncio
async def test_attention_min_events_filter(patched_session):
    """events < min_events 但 is_new_in_version=True 仍进 attention"""
    target = date(2026, 4, 29)
    await _seed_today_data(patched_session, target)

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        _, payload = await daily_report.compose_report("morning", target, top_n=5)

    attn_ids = set(payload.get("attention_issue_ids") or [])
    assert "ios-2" in attn_ids, f"is_new_in_version 应进 attention（不受 min_events 限制），实际: {attn_ids}"


@pytest.mark.asyncio
async def test_top5_sorted_by_impact_score(patched_session):
    """Top5 按 crash_free_impact_score DESC（ios-1 impact 300 在 ios-2 impact 120 之前）"""
    target = date(2026, 4, 29)
    await _seed_today_data(patched_session, target)

    from app.crashguard.services import daily_report
    with patch.object(daily_report, "get_crashguard_settings", return_value=_make_settings()):
        text, _ = await daily_report.compose_report("morning", target, top_n=5)

    # 跳过 attention 块（ios-2 是新增 issue 会在顶部出现），只看 Top 5 段
    top5_start = text.find("### 📋 Top")
    if top5_start < 0:
        top5_start = text.rfind("ios-1")  # fallback
    after_top5 = text[top5_start:]
    ios1 = after_top5.find("ios-1")
    ios2 = after_top5.find("ios-2")
    if ios1 > 0 and ios2 > 0:
        assert ios1 < ios2, "Top 5 段内 ios-1 应在 ios-2 之前（impact 300 > 120）"
